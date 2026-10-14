EXPECTED_HEADER={'buyer_name':'ADITYA BIRLA LIFESTYLE BRANDS LIMITED','factory':'Good Earth Apparels Ltd.','style_number':'WEC015315, WEC015317, WEC015326','department':"MEN'S SHIRTS",'currency':'USD'}
EXPECTED_PO={'po_number':'3110121365','po_date':'2026-03-26','ship_date':'2026-07-20','stated_total_qty':8350,'stated_total_value':70013.5}
LINES=[('WEC015315','5331','EYE SHADOW',7.85,6358.5,810,[32,130,243,218,130,57]),('WEC015317','7459','AE FLORAL',8.5,6885.,810,[32,130,243,218,130,57]),('WEC015317','7200','Berry red',9.1,21203.,2330,[93,373,699,629,373,163]),('WEC015317','7008','CHARCOAL01',9.1,20657.,2270,[91,363,681,613,363,159]),('WEC015326','5401','AE Light Blue',7.,14910.,2130,[85,341,639,575,341,149])]
def checks(o):
    rows=[]
    def check(field,e,a): rows.append({'field':field,'expected':e,'actual':a,'ok':e==a})
    for k,e in EXPECTED_HEADER.items():check(k,e,o.get(k))
    pos=o.get('pos') or [];check('PO count',1,len(pos))
    p=next((x for x in pos if x.get('po_number')=='3110121365'),{})
    for k,e in EXPECTED_PO.items():check(k,e,p.get(k))
    actual=p.get('lines') or [];check('line count',5,len(actual))
    for st,cc,col,rt,nv,q,sz in LINES:
        line=next((x for x in actual if x.get('style')==st and str(x.get('color_code'))==cc),{})
        for k,e in [('style',st),('color_code',cc),('color',col),('rate',rt),('net_value',nv),('order_qty',q)]:check(st+'/'+cc+'/'+k,e,line.get(k))
        for s,v in zip(['35','38','39','40','42','44'],sz):check(st+'/'+cc+'/size/'+s,v,line.get('sizes',{}).get(s))
    return rows
