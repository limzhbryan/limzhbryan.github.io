import urllib.request, urllib.parse, json
UA={"User-Agent":"Mozilla/5.0 (site data job)"}
def get(u):
    try:
        r=urllib.request.urlopen(urllib.request.Request(u,headers=UA),timeout=40); b=r.read()
        print("OK",r.status,u,len(b)); return b
    except Exception as e: print("FAIL",u,e); return b""
hits=[]
for p in range(1,40):
    b=get(f"https://api-production.data.gov.sg/v2/public/api/collections?page={p}")
    try: cs=json.loads(b)["data"]["collections"]
    except Exception: break
    if not cs: break
    for c in cs:
        t=(c.get("name","")+" "+c.get("description","")).lower()
        if "oil" in t or "petrol" in t or "enterprise singapore" in " ".join(c.get("sources",[])).lower():
            print("COLL",c["collectionId"],c["name"],c.get("frequency"),c.get("sources"),c.get("lastUpdatedAt"),c.get("childDatasets"))
for kw in ["oil stocks","oil","petroleum"]:
    b=get("https://tablebuilder.singstat.gov.sg/api/table/resourceid?keyword="+urllib.parse.quote(kw)+"&searchOption=all")
    print(b[:3000].decode("utf8","ignore"))
