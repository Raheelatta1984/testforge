// Portable sequential TestForge runtime. No eval, shell commands, or fixed sleeps.
const { expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const MAX_FILE = 512 * 1024;
function resolve(value, vars) {
  if (typeof value === 'string') return value.replace(/\{\{\s*([\w.-]+)\s*\}\}/g, (_, name) => {
    const v = Object.prototype.hasOwnProperty.call(vars,name) ? vars[name] : process.env['TF_VAR_' + name];
    if (v === undefined || v === null) throw new Error('Missing variable: ' + name);
    return typeof v === 'boolean' ? (v ? 'True' : 'False') : String(v);
  });
  if (Array.isArray(value)) return value.map(v => resolve(v, vars));
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k,v]) => [k,resolve(v,vars)]));
  return value;
}
function url(value) {
  const u = new URL(value);
  if (!['http:', 'https:'].includes(u.protocol) || u.username || u.password) throw new Error('Only HTTP(S) URLs without credentials are supported');
  return value;
}
function locator(page, o) {
  let root = page;
  for (const frame of o.frames || []) root = root.frameLocator(frame);
  const exact = o.exact !== false;
  switch (o.kind || 'css') {
    case 'role': return root.getByRole(o.primary, o.name === undefined ? {} : {name:o.name,exact});
    case 'label': return root.getByLabel(o.primary,{exact});
    case 'text': return root.getByText(o.primary,{exact});
    case 'test_id': return root.getByTestId(o.primary);
    case 'placeholder': return root.getByPlaceholder(o.primary,{exact});
    default: return root.locator(o.primary);
  }
}
async function replay(initialPage, steps, outputDir, vars = {}, state = null) {
  const pages = state?.pages || new Map([['main', initialPage]]), routes = state?.routes || [];
  let page = state?.page || initialPage, count = 0;
  if(state){state.pages=pages;state.routes=routes;}
  fs.mkdirSync(outputDir, {recursive:true});
  try {
    for (const step of steps) {
      const repeat = step.repeat_count || step.repeat || 1;
      if (!Number.isInteger(repeat) || repeat < 1 || repeat > 100) throw new Error('Invalid repeat count');
      for (let i = 0; i < repeat; i++) {
        if (++count > 2000) throw new Error('Expanded action limit exceeded');
        const action = step.action;
        const value = ['capture_text','save_variable','switch_page','close_page'].includes(action) ? step.value : resolve(step.value || '', vars);
        const o = resolve(step.selector || {}, vars), timeout = o.timeout_ms || 3000;
        if (o.page) { if (!pages.has(o.page)) throw new Error('Unknown page'); page = pages.get(o.page); }
        const l = o.primary ? locator(page,o) : null;
        switch (action) {
          case 'navigate': await page.goto(url(value),{waitUntil:'domcontentloaded',timeout:o.timeout_ms || 20000}); break;
          // A step may target an element, a recorded point (x/y) or the focused
          // element; the same three shapes the Python runtime accepts.
          case 'click':
            if (l) await l.click({timeout});
            else if (o.x !== undefined && o.y !== undefined) await page.mouse.click(o.x,o.y);
            else throw new Error('Click requires a locator or recorded coordinates');
            break;
          case 'right_click':
            if (l) await l.click({button:'right',timeout});
            else if (o.x !== undefined && o.y !== undefined) await page.mouse.click(o.x,o.y,{button:'right'});
            else throw new Error('Right click requires a locator or recorded coordinates');
            break;
          case 'dblclick':
            if (l) await l.dblclick({timeout});
            else if (o.x !== undefined && o.y !== undefined) await page.mouse.dblclick(o.x,o.y);
            else throw new Error('Double click requires a locator or recorded coordinates');
            break;
          case 'hover':
            if (l) await l.hover({timeout});
            else if (o.x !== undefined && o.y !== undefined) await page.mouse.move(o.x,o.y);
            else throw new Error('Hover requires a locator or recorded coordinates');
            break;
          case 'fill': if (l) await l.fill(value,{timeout}); else await page.keyboard.insertText(value); break;
          case 'type': if (l) await l.pressSequentially(value,{delay:0,timeout}); else await page.keyboard.type(value,{delay:0}); break;
          case 'press': if (l) await l.press(value,{timeout}); else await page.keyboard.press(value); break;
          case 'check': case 'uncheck': await l[action]({timeout}); break;
          case 'select_option': await l.selectOption(o.by === 'label' ? {label:value} : {value},{timeout}); break;
          case 'drag_to': await l.dragTo(locator(page,o.target),{timeout}); break;
          case 'scroll': await l.scrollIntoViewIfNeeded({timeout}); await l.evaluate((el,y)=>el.scrollBy(0,y),Number(value)); break;
          case 'wait_visible': case 'wait_hidden': await l.waitFor({state:action.slice(5),timeout}); break;
          case 'assert_text': await expect(l).toHaveText(value,{timeout}); break;
          case 'assert_contains': await expect(l).toContainText(value,{timeout}); break;
          case 'assert_value': await expect(l).toHaveValue(value,{timeout}); break;
          case 'assert_count': await expect(l).toHaveCount(Number(value),{timeout}); break;
          case 'assert_url': await expect(page).toHaveURL(value,{timeout}); break;
          case 'assert_title': await expect(page).toHaveTitle(value,{timeout}); break;
          case 'assert_visible': await expect(l).toBeVisible({timeout}); break;
          case 'assert_hidden': await expect(l).toBeHidden({timeout}); break;
          case 'assert_enabled': await expect(l).toBeEnabled({timeout}); break;
          case 'assert_disabled': await expect(l).toBeDisabled({timeout}); break;
          case 'assert_checked': await expect(l).toBeChecked({timeout}); break;
          case 'assert_unchecked': await expect(l).not.toBeChecked({timeout}); break;
          case 'save_variable': vars[value] = await l.inputValue({timeout}); break;
          case 'capture_text': vars[value] = await l.innerText({timeout}); break;
          case 'new_page': case 'open_popup': {
            if (pages.has(o.alias) || pages.size >= 4) throw new Error('Page alias exists or page limit reached');
            if (action === 'new_page') { page = await page.context().newPage(); pages.set(o.alias,page); await page.goto(url(value),{waitUntil:'domcontentloaded',timeout}); }
            else { const [popup] = await Promise.all([page.waitForEvent('popup',{timeout}), l.click({timeout})]); page = popup; pages.set(o.alias,page); await page.waitForLoadState('domcontentloaded',{timeout}); }
            break;
          }
          case 'switch_page': if (!pages.has(value) || pages.get(value).isClosed()) throw new Error('Unknown or closed page'); page=pages.get(value); break;
          case 'close_page': if (value === 'main' || !pages.has(value)) throw new Error('Cannot close page'); await pages.get(value).close(); pages.delete(value); page=initialPage; break;
          case 'dialog': {
            let observed;
            const handler = async d => { observed = {message:d.message(),type:d.type()}; if ((o.decision || 'accept') === 'accept') await d.accept(value); else await d.dismiss(); };
            page.once('dialog',handler);
            try { await Promise.all([page.waitForEvent('dialog',{timeout}),l.click({timeout})]);
              if (o.message !== undefined && o.message !== observed.message) throw new Error('Dialog message did not match');
              if (o.dialog_type !== undefined && o.dialog_type !== observed.type) throw new Error('Dialog type did not match');
            } finally { page.removeListener('dialog',handler); }
            break;
          }
          case 'upload_file': {
            const buffer=Buffer.from(o.base64 || '', 'base64');
            if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,100}$/.test(o.filename || '') || buffer.length > MAX_FILE) throw new Error('Invalid upload fixture');
            await l.setInputFiles({name:o.filename,mimeType:o.mime_type || 'application/octet-stream',buffer},{timeout}); break;
          }
          case 'download': {
            const [d]=await Promise.all([page.waitForEvent('download',{timeout}),l.click({timeout})]);
            try { if (value && d.suggestedFilename() !== value) throw new Error('Download filename did not match');
              const p=await d.path(); if (!p || fs.statSync(p).size > MAX_FILE) throw new Error('Download missing or too large');
              await d.saveAs(path.join(outputDir,'download-'+crypto.randomUUID()+'.bin'));
            } finally { await d.delete(); } break;
          }
          case 'api_request': {
            const r=await page.context().request.fetch(url(value),{method:o.method || 'GET',headers:o.headers || {},data:o.body,timeout,failOnStatusCode:false,maxRedirects:0});
            try { if (r.status() !== (o.status || 200)) throw new Error('API HTTP status did not match');
              if ('json_path' in o || 'capture' in o || 'expected' in o) { const b=await r.body(); if(b.length>MAX_FILE) throw new Error('API response too large');
                let actual=JSON.parse(b); for(const part of (o.json_path ? String(o.json_path).split('.') : [])) actual=actual[part];
                if ('expected' in o) expect(actual).toEqual(o.expected);
                if(o.capture) vars[o.capture]=actual;
              }
            } finally { await r.dispose(); } break;
          }
          case 'mock_route': {
            if(routes.length>=10) throw new Error('Network mock limit reached');
            const handler=route=>o.abort ? route.abort() : route.fulfill({status:o.status || 200,contentType:o.content_type || 'application/json',body:String(o.body || '{}')});
            await page.route(value,handler); routes.push([page,value,handler]); break;
          }
          case 'clear_routes': for(const [p,pattern,handler] of routes) if(!p.isClosed()) await p.unroute(pattern,handler); routes.length=0; break;
          default: throw new Error('Unsupported action: '+action);
        }
      }
    }
  } finally {
    if(state) state.page=page;
    if(!state?.keepOpen){
      for(const [p,pattern,handler] of routes) if(!p.isClosed()) await p.unroute(pattern,handler);
      for(const [alias,p] of pages) if(alias!=='main' && !p.isClosed()) await p.close();
    }
  }
}
function launchOptions() {
  const options = {headless:true,args:['--no-sandbox','--disable-dev-shm-usage']};
  if(process.env.TF_CHROMIUM_PATH) options.executablePath=process.env.TF_CHROMIUM_PATH;
  if(process.env.TF_CHROMIUM_LIBS) options.env={...process.env,LD_LIBRARY_PATH:process.env.TF_CHROMIUM_LIBS};
  return options;
}
module.exports = {replay, launchOptions, locator, resolve};
