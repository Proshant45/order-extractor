"""Optional AI reader backed by the opencode CLI (`opencode run`), EXTRACTOR=opencode.
Auth: OPENCODE_API_KEY env (OpenCode Zen key) or ~/.local/share/opencode/auth.json from `opencode auth login`.
`opencode run` keeps running after the answer, so we stream JSON events and kill it on the final step_finish."""
from __future__ import annotations
import json, os, resource, shutil, signal, subprocess, tempfile, time
from pathlib import Path

def binary():
    for p in (os.getenv("OPENCODE_BIN"), shutil.which("opencode"), str(Path.home() / ".opencode/bin/opencode"),
              str(Path(__file__).resolve().parent.parent / "bin/opencode")):
        if p and Path(p).exists():
            return p
    return None

def available():
    return binary() is not None

def _limits():
    os.setsid()  # own process group so we can kill opencode and any children together
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

def _rss_mb(pid: int) -> float:
    """RSS of the whole process group (Bun reserves huge virtual memory, so RLIMIT_AS can't be used; watch RSS instead)."""
    tot = 0
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            st = (d / "stat").read_text().rsplit(")", 1)[1].split()
            if int(st[2]) == pid:   # pgid
                tot += int(st[21]) * 4096
        except (OSError, IndexError, ValueError):
            pass
    return tot / 1048576

NO_TOOLS = ("\n\nIMPORTANT: you must NOT call any tool (no read, bash, glob, grep, edit, webfetch). Everything you need "
            "is in this message. Respond immediately with the final JSON object only.")

def run(prompt: str, files: list[Path] = (), model: str | None = None, timeout: int | None = None) -> str:
    """One retry when the model wanders into a tool call and the run ends without an answer."""
    # Free tier rejects custom agents / disabled tools, so keep the default agent and remove the reason to use
    # tools: inline text files in the message and only attach images.
    files = list(files)
    inline = [f for f in files if Path(f).suffix.lower() in (".txt", ".csv", ".md", ".json") and Path(f).stat().st_size < 120_000]
    if inline:
        files = [f for f in files if f not in inline]
        prompt = prompt + "".join(f"\n\n=== DOCUMENT TEXT ({Path(f).name}) ===\n" + Path(f).read_text(errors="replace") + "\n=== END ===" for f in inline)
    prompt += NO_TOOLS
    try:
        return _run(prompt, files, model, timeout)
    except RuntimeError as e:
        if "tool_use" not in str(e) and "tool-calls" not in str(e):
            raise
        return _run(prompt + "\nThe previous attempt called a tool and was discarded. Do NOT call tools. JSON only.", files, model, timeout)

def _run(prompt: str, files: list[Path] = (), model: str | None = None, timeout: int | None = None) -> str:
    exe = binary()
    if not exe:
        raise RuntimeError("opencode binary not found (set OPENCODE_BIN or install in build step)")
    model = model or os.getenv("OPENCODE_MODEL", "opencode/mimo-v2.6-flash-free")
    timeout = timeout or int(os.getenv("OPENCODE_TIMEOUT_S", "300"))
    work = tempfile.mkdtemp(prefix="oc_")
    cmd = [exe, "run", "-m", model, "--format", "json", "--dir", work, prompt]
    for f in files:
        cmd += ["-f", str(f)]
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "OPENCODE_API_KEY", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "LANG", "TMPDIR")}
    small = os.getenv("OPENCODE_SMALL_MODEL", model)   # title agent defaults to a paid model; keep it on the free one
    env.update(OPENCODE_CONFIG_CONTENT=json.dumps({"small_model": small, "autoupdate": False, "share": "disabled"}),
               OPENCODE_DISABLE_AUTOUPDATE="1", OPENCODE_DISABLE_LSP_DOWNLOAD="1", OPENCODE_DISABLE_CLAUDE_CODE="1")
    # NB: Zen free tier returns 403 "only from within OpenCode" if any custom permission config is set; isolation = empty temp dir + minimal env
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=work, env=env,
                         preexec_fn=_limits, text=True)
    texts, err, deadline, seen = [], None, time.monotonic() + timeout, []
    try:
        import selectors
        sel = selectors.DefaultSelector(); sel.register(p.stdout, selectors.EVENT_READ)
        mem_cap = int(os.getenv("OPENCODE_MEM_MB", "350"))
        while time.monotonic() < deadline:
            if _rss_mb(p.pid) > mem_cap:
                raise MemoryError(f"opencode exceeded {mem_cap} MB RSS")
            if not sel.select(timeout=1):
                if p.poll() is not None: break
                continue
            line = p.stdout.readline()
            if not line:
                break
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            t = ev.get("type"); seen.append(t if t != "step_finish" else f"finish:{ev.get('part',{}).get('reason')}")
            if t == "text":
                texts.append(ev["part"].get("text", ""))
            elif t == "error":
                err = json.dumps(ev.get("error", ev))[:300]; break
            elif t == "step_finish" and (ev.get("part", {}).get("reason") in ("stop", "end_turn", None)) and texts:
                break
            elif t == "step_finish" and ev.get("part", {}).get("reason") in ("stop", "end_turn") and not texts:
                err = "model finished without text (events: " + ",".join(seen[-8:]) + ")"; break
        else:
            raise TimeoutError(f"opencode timed out after {timeout}s")
    finally:
        try: os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        p.wait(); shutil.rmtree(work, ignore_errors=True)
    if err:
        raise RuntimeError(f"opencode error: {err}")
    if not texts:
        raise RuntimeError("opencode returned no text (events: " + ",".join(seen[-8:]) + f", rc={p.returncode})")
    return "".join(texts)
