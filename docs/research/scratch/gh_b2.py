import json, urllib.request
def get(u):
    r=urllib.request.Request(u,headers={"User-Agent":"gloss-research"})
    return json.load(urllib.request.urlopen(r))
for repo,nums in [("ramjke/Translumo",[39,228,174,3]),("HIllya51/LunaTranslator",[1663,533,1131,1358])]:
    for n in nums:
        i=get(f"https://api.github.com/repos/{repo}/issues/{n}")
        print("###",repo,n,i["state"],i["title"]); print((i["body"] or "")[:350].replace("\n"," "))
        cs=get(f"https://api.github.com/repos/{repo}/issues/{n}/comments?per_page=4")
        for c in cs[:3]: print("  >>",c["user"]["login"],":",c["body"][:260].replace("\n"," "))
