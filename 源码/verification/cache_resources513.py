"""One-time build resource preparation; runtime never fetches external images."""
from pathlib import Path
import concurrent.futures,hashlib,json,shutil,urllib.request,urllib.error
ROOT=Path(__file__).resolve().parents[2]
assets=ROOT.parent/'GatewayCapture-V5.1.7-完整独立包'/'程序/assets'
out=ROOT/'资料'
ids=json.loads((out/'三角洲物资映射.json').read_text(encoding='utf-8'))
items=assets/'items';items.mkdir(exist_ok=True)
reference=ROOT.parent/'寻宝鼠html源码/assets/Weapon'
for p in reference.glob('*.png'):shutil.copy2(p,items/p.name)
def fetch(item):
 target=items/(item+'.png');url='https://playerhub.df.qq.com/playerhub/60004/object/'+item+'.png'
 if target.exists():return {'id':item,'source':'寻宝鼠/assets/Weapon','bytes':target.stat().st_size}
 for attempt in range(2):
  try:
   with urllib.request.urlopen(url,timeout=12) as response:data=response.read()
   assert data.startswith(b'\x89PNG') or data.startswith(b'\xff\xd8')
   target.write_bytes(data);return {'id':item,'source':url,'bytes':len(data)}
  except Exception as error:last=str(error)
 return {'id':item,'source':url,'error':last}
results=[]
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
 for i,r in enumerate(executor.map(fetch,ids),1):
  results.append(r)
  if i%200==0:print('Item images',i,'/',len(ids),flush=True)
# Original source URLs omit some overview tiles. Generate them from the same map,
# without changing bounds, pixel geometry, tile indexing or projection.
from PIL import Image
generated=[]
for directory in (assets/'maps2d').iterdir():
 for z in (1,0):
  for x in range(2**z):
   for y in range(2**z):
    target=directory/f'{z}_{x}_{y}.jpg'
    if target.exists():continue
    mosaic=Image.new('RGB',(512,512))
    for dx in (0,1):
     for dy in (0,1):
      with Image.open(directory/f'{z+1}_{2*x+dx}_{2*y+dy}.jpg') as im:mosaic.paste(im.convert('RGB').resize((256,256)),(256*dx,256*dy))
    mosaic.resize((256,256),Image.Resampling.LANCZOS).save(target,quality=92);generated.append(str(target.relative_to(ROOT)))
(out/'本地资源准备.json').write_text(json.dumps({'items':results,'generatedOverviewTiles':generated,'runtimeExternalFallback':False},ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'itemsSaved':sum('error' not in r for r in results),'itemsMissing':sum('error' in r for r in results),'overviewTilesGenerated':len(generated)},ensure_ascii=False),flush=True)
