"""Browser regression with explicit API fixtures. Run against a local Next server; requires Python playwright and Edge."""
import asyncio,json
from pathlib import Path
from urllib.parse import urlparse,parse_qs
from playwright.async_api import async_playwright
ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'artifacts/research-discovery';OUT.mkdir(parents=True,exist_ok=True)
ID='11111111-1111-4111-8111-111111111111'
match=dict(globalInstrumentId=ID,companyName='Chennai Petroleum Corporation Ltd.',canonicalSymbol='CHENNPETRO',symbol='CHENNPETRO',exchange='NSE',isin='INE178A01016',country='IN',region='INDIA',assetType='EQUITY')
company=dict(instrumentId=ID,companyName=match['companyName'],ticker='CHENNPETRO',exchange='NSE',isin=match['isin'],assetType='EQUITY',status='RESEARCH_PENDING',valuation=dict(state='UNAVAILABLE',reason='No persisted valuation'),sourceDiversity={},sourceCount=0,documentCount=0,ownershipIncreases=[],shareholdingChanges=[],currentQuarterCatalysts=[])
watchlist=dict(watchlistId='list-india',region='INDIA',name='WATCHLIST-IND',systemDefault=True,instrumentCount=0)
async def main():
 calls=[]; saved=[]; browser_requests=[]
 async with async_playwright() as pw:
  browser=await pw.chromium.launch(channel='msedge',headless=True)
  page=await browser.new_page(viewport=dict(width=1440,height=1050))
  await page.add_init_script("localStorage.setItem('aip.accessToken','fixture-token'); localStorage.setItem('aip.user',JSON.stringify({userId:'fixture',displayName:'Research validation',email:'fixture@example.test'}));")
  page.on('request', lambda request: browser_requests.append(dict(method=request.method,url=request.url)))
  page.on('pageerror',lambda error: print('PAGEERROR',str(error).splitlines()[0]))
  async def route(r):
   u=urlparse(r.request.url); path=u.path; calls.append(dict(method=r.request.method,url=r.request.url))
   data=[];status=200
   if path.endswith('/dashboard'): data=dict(currencyTotals={},portfolios=[],incompleteValuationPortfolioIds=[])
   elif path.endswith('/instruments/search'):
    q=parse_qs(u.query).get('q',[''])[0]
    if q=='slow': await asyncio.sleep(.8); data=[dict(match,companyName='Stale result')]
    elif q=='nothing': data=[]
    else: data=[match]
   elif path.endswith('/watchlists/default/ensure'): data=watchlist
   elif path.endswith('/watchlists/list-india/instruments'):
    saved.append(r.request.post_data_json['globalInstrumentId']);data={'globalInstrumentId':ID}
   elif path.endswith('/watchlists'): data=[watchlist]
   elif path.endswith('/watchlists/list-india/research'): data={'watchlist':watchlist,'instruments':[dict(globalInstrumentId=i,company=company,held=False) for i in saved]}
   elif '/readiness/' in path: data=dict(globalInstrumentId=ID,overallStatus='MISSING',overallCompletenessPct=0,criticalCompletenessPct=0,confidence='LOW',confidencePct=0,requirements=[],generatedAt='2026-09-12T00:00:00Z')
   elif path.endswith('/presentation'): data=company
   elif path.endswith('/summary'): status=404;data={'message':'No persisted summary'}
   elif 'ensure' in path: data={}
   await r.fulfill(status=status,json=data)
  await page.route('**/api/v1/**',route)
  async def research():
   await page.goto('http://localhost:3000'); await page.get_by_role('button',name='Research',exact=True).click();await page.wait_for_timeout(700)
  await research()
  await page.screenshot(path=str(OUT/'after.png'),full_page=True)
  search=page.locator('#research-stock-query')
  searches=lambda:[c for c in calls if '/instruments/search?' in c['url']]
  await search.fill('c');await page.wait_for_timeout(400)
  await search.fill('ch');await page.wait_for_timeout(400)
  assert len(searches())==0,searches()
  await search.fill('che');await page.wait_for_timeout(500)
  assert len(searches())==1,searches()
  await page.locator('#research-search-listbox [role=option]').first.wait_for()
  await page.screenshot(path=str(OUT/'after-results.png'),full_page=True)
  await search.fill(' che ');await page.wait_for_timeout(400);assert len(searches())==1
  await search.fill('slow');await page.wait_for_timeout(400);await search.fill('nothing');await page.wait_for_timeout(1100)
  assert await page.get_by_text('No stocks found.',exact=True).is_visible()
  assert not await page.get_by_text('Stale result',exact=True).is_visible()
  await search.fill('che');await page.wait_for_timeout(400);assert len(searches())==3
  before_selection=len(calls)
  await search.press('ArrowDown');await search.press('Enter');await page.wait_for_timeout(600)
  assert await page.get_by_role('dialog').count() == 1
  assert all(c['method'] == 'GET' for c in calls[before_selection:]), calls[before_selection:]
  assert any('/companies/'+ID+'/presentation?' in c['url'] for c in calls[before_selection:])
  close=page.get_by_role('button',name='Close research readiness')
  if await close.count():await close.click()
  else: await page.keyboard.press('Escape')
  await page.get_by_role('button',name='Add to WATCHLIST-IND',exact=True).click()
  await page.get_by_role('button',name='Saved to WATCHLIST-IND',exact=True).wait_for()
  await research();await page.locator('#research-stock-query').fill('che');await page.wait_for_timeout(450);await page.locator('#research-stock-query').press('Enter');await page.wait_for_timeout(500)
  if await close.count():await close.click()
  else:await page.keyboard.press('Escape')
  await page.get_by_role('button',name='Saved to WATCHLIST-IND',exact=True).wait_for()
  assert await page.get_by_text('Public company research · Not held',exact=True).is_visible()
  assert not await page.get_by_text('Sector Performance',exact=True).is_visible()
  assert not any('nseindia.com' in c['url'] or 'yahoo' in c['url'] or 'mcp' in c['url'] for c in browser_requests)
  await page.screenshot(path=str(OUT/'after-saved-reload.png'),full_page=True)
  await page.get_by_role('button',name='Open company research',exact=True).click()
  drawer=page.get_by_role('dialog')
  await drawer.wait_for()
  for label in ['Quantity', 'Average Cost', 'Cost Basis', 'Market Value', 'Unrealized P/L']:
   assert not await drawer.get_by_text(label,exact=True).count()
  await page.screenshot(path=str(OUT/'after-public-company.png'),full_page=True)
  (OUT/'browser-network.json').write_text(json.dumps(browser_requests,indent=2))
  print('PASS browser debounce, cache, stale response, keyboard, canonical selection, watchlist save/reload; fixture APIs')
  await browser.close()
asyncio.run(main())
