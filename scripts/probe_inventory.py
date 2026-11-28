import urllib.request, urllib.parse, json, sys
UA={"User-Agent":"Mozilla/5.0 (site data job)"}
def get(u):
    try:
        r=urllib.request.urlopen(urllib.request.Request(u,headers=UA),timeout=40); b=r.read()
        print("OK",r.status,u,len(b)); return b
    except Exception as e: print("FAIL",u,e); return b""
for t in ["table1","table4","table9"]:
    b=get(f"https://ir.eia.gov/wpsr/{t}.csv"); print(b[:2500].decode("latin1","ignore")); print("-----")
b=get("https://ir.eia.gov/wpsr/wpsrsummary.pdf"); print(b[:200])
for q in ["oil stocks","onshore oil","weekly oil"]:
    b=get("https://api-production.data.gov.sg/v2/public/api/datasets?query="+urllib.parse.quote(q) if False else "https://data.gov.sg/api/action/package_search?q="+q.replace(" ","+"))
    print(b[:3000].decode("utf8","ignore")); print("-----")
b=get("https://api-production.data.gov.sg/v2/public/api/collections?page=1"); print(b[:500])
