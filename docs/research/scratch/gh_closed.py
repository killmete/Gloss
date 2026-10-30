import json, urllib.request
def get(u):
    r=urllib.request.Request(u,headers={"User-Agent":"gloss-research"})
    return json.load(urllib.request.urlopen(r))
for repo in ["ramjke/Translumo","HIllya51/LunaTranslator"]:
    l=[i for i in get(f"https://api.github.com/repos/{repo}/issues?state=closed&sort=comments&direction=desc&per_page=40") if "pull_request" not in i]
    print("==",repo)
    for i in l[:18]: print(i["number"],i["comments"],i["created_at"][:10],i["title"][:100])
d=json.load(open("../data/prior_art_issues_2026-10-30.json"))
import re
kw=re.compile(r"antivirus|virus|defender|fullscreen|full screen|black|dpi|scale|overlay|flicker|crash|cpu|memory|ram|duplicate|repeat|delay|slow|hotkey",re.I)
for k in ["translumo","screentranslator"]:
    print("-- kw",k)
    for i in d[k]:
        if kw.search(i["t"]): print(i["n"],i["t"][:100])
