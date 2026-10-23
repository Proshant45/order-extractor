"""AI reader backed by the Codex CLI (`codex exec`), EXTRACTOR=codex (box worker / local only).
Auth: the CLI's own login in $CODEX_HOME (default ~/.codex), e.g. `codex login` with a ChatGPT account,
or CODEX_API_KEY / OPENAI_API_KEY for API-key billing. Runs non-interactively in a throwaway directory with a
read-only sandbox; we stream `--json` events and accept the answer only on `turn.completed`."""
from __future__ import annotations
import json, os, shutil, signal, subprocess, tempfile, time
from pathlib import Path

DEFAULT_MODEL = "gpt-6-luna"   # fastest model the ChatGPT login allows (Oct 2026); override with CODEX_MODEL

def default_model() -> str:
    return os.getenv("CODEX_MODEL", DEFAULT_MODEL)

def binary():
    for p in (os.getenv("CODEX_BIN"), shutil.which("codex"), str(Path.home() / ".local/bin/codex"),
              str(Path(__file__).resolve().parent.parent / "bin/codex")):
        if p and Path(p).exists():
            return p
    return None

def available():
    return binary() is not None

def _rss_mb(pid: int) -> float:
    """RSS of the whole process group."""
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

NO_TOOLS = ("\n\nIMPORTANT: do NOT run any command or tool. Everything you need is in this message. "
            "Respond immediately with the final JSON object only.")

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif")

def run(prompt: str, files: list[Path] = (), model: str | None = None, timeout: int | None = None, on_event=None, stop_event=None) -> str:
    """Text files are inlined into the prompt, images are attached with -i. One retry when the run ends without an answer."""
    files = list(files)
    inline = [f for f in files if Path(f).suffix.lower() not in IMAGE_EXT]
    images = [f for f in files if f not in inline]
    prompt = prompt + "".join(f"\n\n=== DOCUMENT TEXT ({Path(f).name}) ===\n" + Path(f).read_text(errors="replace") + "\n=== END ===" for f in inline)
    prompt += NO_TOOLS
    retries = int(os.getenv("CODEX_RETRIES", "1"))
    for attempt in range(retries + 1):
        try:
            return _run(prompt, images, model, timeout, on_event=on_event, stop_event=stop_event)
        except RuntimeError as e:
            transient = any(s in str(e) for s in ("without a completion", "returned no text", "stream", "429", "rate limit", "overloaded"))
            if attempt >= retries or not transient:
                raise
            time.sleep(min(30, 3 * 2 ** attempt))

def _run(prompt: str, images: list[Path] = (), model: str | None = None, timeout: int | None = None, on_event=None, stop_event=None) -> str:
    exe = binary()
    if not exe:
        raise RuntimeError("codex binary not found (set CODEX_BIN or install the Codex CLI)")
    model = model or default_model()
    timeout = timeout or int(os.getenv("CODEX_TIMEOUT_S", "300"))
    work = tempfile.mkdtemp(prefix="cx_")
    cmd = [exe, "exec", "--json", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "--ignore-rules",
           "-s", "read-only", "-C", work, "-m", model,
           "-c", f'model_reasoning_effort="{os.getenv("CODEX_REASONING", "low")}"', "-c", "web_search=\"disabled\""]
    for f in images:
        cmd += ["-i", str(f)]
    cmd.append("-")   # prompt on stdin (no argv size limit, not visible in ps)
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "CODEX_HOME", "CODEX_API_KEY", "OPENAI_API_KEY", "LANG", "TMPDIR",
                                                          "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "SSL_CERT_FILE", "HTTPS_PROXY", "NO_PROXY")}
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=work, env=env,
                         start_new_session=True)
    try:
        p.stdin.write(prompt.encode()); p.stdin.close()
    except BrokenPipeError:
        pass
    texts, err, deadline, seen = [], None, time.monotonic() + timeout, []
    completed, pending, output_bytes = False, b"", 0
    try:
        import selectors
        sel = selectors.DefaultSelector(); sel.register(p.stdout, selectors.EVENT_READ)
        mem_cap = int(os.getenv("CODEX_MEM_MB", "1400"))
        os.set_blocking(p.stdout.fileno(), False)
        while time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set(): raise InterruptedError("worker shutting down; model process stopped")
            if _rss_mb(p.pid) > mem_cap:
                raise MemoryError(f"codex exceeded {mem_cap} MB RSS")
            if not sel.select(timeout=min(1, max(0, deadline-time.monotonic()))):
                if p.poll() is not None: break
                continue
            chunk = os.read(p.stdout.fileno(), 65536)
            if not chunk:
                break
            output_bytes += len(chunk)
            if output_bytes > 8 * 1024 * 1024:
                raise RuntimeError("codex output exceeded 8 MB")
            pending += chunk
            while b"\n" in pending:
                if time.monotonic() >= deadline: raise TimeoutError(f"codex timed out after {timeout}s")
                line, pending = pending.split(b"\n", 1)
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if on_event:
                    try: on_event(ev)
                    except Exception: pass
                    if time.monotonic() >= deadline: raise TimeoutError(f"codex timed out after {timeout}s")
                t = ev.get("type"); item = ev.get("item") or {}
                seen.append(t if t != "item.completed" else f"item:{item.get('type')}")
                if t == "item.completed" and item.get("type") == "agent_message":
                    texts = [item.get("text", "")]   # last agent message wins
                elif t in ("error", "turn.failed"):
                    e = ev.get("error", ev); err = (e.get("message") if isinstance(e, dict) else str(e)) or json.dumps(ev)
                    err = str(err)[:300]; break
                elif t == "turn.completed":
                    if texts and texts[0].strip():
                        completed = True
                    else:
                        err = "model finished without text (events: " + ",".join(seen[-8:]) + ")"
                    break
            if completed or err:
                break
        else:
            raise TimeoutError(f"codex timed out after {timeout}s")
    finally:
        if "sel" in locals(): sel.close()
        try: os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        p.wait(); p.stdout.close(); shutil.rmtree(work, ignore_errors=True)
    if err:
        raise RuntimeError(f"codex error: {err}")
    if not completed:
        raise RuntimeError("codex exited without a completion event (events: " + ",".join(seen[-8:]) + ")")
    return "".join(texts)
