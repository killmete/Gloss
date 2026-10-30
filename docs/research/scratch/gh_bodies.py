import json, urllib.request
def get(u):
    r=urllib.request.Request(u,headers={"User-Agent":"gloss-research"})
    return json.load(urllib.request.urlopen(r))
for repo,nums in [("ramjke/Translumo",[7,20,234,84,125]),("OneMoreGres/ScreenTranslator",[152,5]),("microsoft/PowerToys",[21676,25336,31992,35614])]:
    for n in nums:
        i=get(f"https://api.github.com/repos/{repo}/issues/{n}")
        print("###",repo,n,i["state"],i["title"]); print((i["body"] or "")[:550].replace("\n"," ")); 
        if i["comments"]:
            cs=get(f"https://api.github.com/repos/{repo}/issues/{n}/comments?per_page=3")
            for c in cs[:2]: print("  >>",c["user"]["login"],":",c["body"][:300].replace("\n"," "))
