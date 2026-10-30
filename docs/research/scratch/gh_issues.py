import json, urllib.request, urllib.parse, sys
def get(u):
    r=urllib.request.Request(u,headers={"User-Agent":"gloss-research"})
    return json.load(urllib.request.urlopen(r))
def issues(repo, extra="", n=100, state="open", sort="comments"):
    u=f"https://api.github.com/repos/{repo}/issues?state={state}&sort={sort}&direction=desc&per_page={n}{extra}"
    return [i for i in get(u) if "pull_request" not in i]
allout={}
for name,repo,extra in [("translumo","ramjke/Translumo",""),("luna","HIllya51/LunaTranslator",""),("screentranslator","OneMoreGres/ScreenTranslator",""),("powertoys_textextractor","microsoft/PowerToys","&labels="+urllib.parse.quote("Product-Text Extractor"))]:
    try:
        iss=issues(repo,extra)
    except Exception as e:
        print(name,"ERR",e); continue
    allout[name]=[{"n":i["number"],"t":i["title"],"c":i["comments"],"u":i["updated_at"][:10],"cr":i["created_at"][:10]} for i in iss]
    print("==",name,len(iss))
    for i in allout[name][:22]: print(i["n"],i["c"],i["cr"],i["t"][:110])
json.dump(allout,open("../data/prior_art_issues_2026-10-30.json","w"),indent=1,ensure_ascii=False)
