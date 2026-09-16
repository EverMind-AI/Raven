import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'
const UIWEB = join(dirname(fileURLToPath(import.meta.url)), '..', '..')
const SRC = join(UIWEB, 'src', 'legacy')
const py = readFileSync(join(UIWEB, 'build.py'), 'utf8')
const manifest = (n) => [...py.match(new RegExp(`_${n}_PARTS = \\[([\\s\\S]*?)\\]`))[1].matchAll(/"([^"]+)"/g)].map((x) => x[1])
function concat(dir, parts) { let text=''; const spans=[]
  for (const p of parts) { const t=readFileSync(join(SRC,dir,p),'utf8'); spans.push({file:`${dir}/${p}`,start:text.length,end:text.length+t.length}); text+=t }
  if (text.endsWith('\n')) { text=text.slice(0,-1); spans[spans.length-1].end-=1 }
  return {text,spans} }
const seam=concat('seam',manifest('SEAM')), demo=concat('demo',manifest('DEMO')), live=concat('live',manifest('LIVE'))
const off=seam.text.length+1
const layers={ demo:{text:seam.text+'\n'+demo.text,spans:[...seam.spans,...demo.spans.map(s=>({file:s.file,start:s.start+off,end:s.end+off}))]}, live:{text:live.text,spans:live.spans} }
function bn(nm,out){ if(!nm)return; if(ts.isIdentifier(nm))out.add(nm.text); else if(nm.elements)for(const e of nm.elements){if(ts.isOmittedExpression(e))continue;bn(e.name,out)} }
function declaredIn(scope){ const out=new Set()
  if(ts.isFunctionLike(scope)){for(const p of scope.parameters||[])bn(p.name,out); if(scope.name&&ts.isIdentifier(scope.name)&&(ts.isFunctionExpression(scope)||ts.isFunctionDeclaration(scope)))out.add(scope.name.text)}
  if(ts.isCatchClause(scope)&&scope.variableDeclaration)bn(scope.variableDeclaration.name,out)
  const stmts=ts.isBlock(scope)||ts.isSourceFile(scope)?scope.statements:(ts.isFunctionLike(scope)&&scope.body&&ts.isBlock(scope.body))?scope.body.statements:(ts.isCaseClause(scope)||ts.isDefaultClause(scope))?scope.statements:null
  if(stmts)for(const st of stmts){if(ts.isVariableStatement(st))for(const d of st.declarationList.declarations)bn(d.name,out); else if((ts.isFunctionDeclaration(st)||ts.isClassDeclaration(st))&&st.name)out.add(st.name.text)}
  const i=scope.initializer
  if((ts.isForStatement(scope)||ts.isForOfStatement(scope)||ts.isForInStatement(scope))&&i&&ts.isVariableDeclarationList(i))for(const d of i.declarations)bn(d.name,out)
  return out }
const isScope=(n)=>ts.isFunctionLike(n)||ts.isBlock(n)||ts.isSourceFile(n)||ts.isCatchClause(n)||ts.isForStatement(n)||ts.isForOfStatement(n)||ts.isForInStatement(n)||ts.isCaseClause(n)||ts.isDefaultClause(n)
const STD = new Set(['window','document','console','Math','JSON','Object','Array','String','Number','Boolean','Date','Promise','Set','Map','WeakMap','WeakSet','Error','TypeError','RangeError','Symbol','RegExp','Intl','URL','URLSearchParams','navigator','location','history','localStorage','sessionStorage','fetch','setTimeout','clearTimeout','setInterval','clearInterval','queueMicrotask','requestAnimationFrame','cancelAnimationFrame','MutationObserver','ResizeObserver','IntersectionObserver','Event','CustomEvent','MouseEvent','KeyboardEvent','Blob','File','FileReader','FormData','Headers','Request','Response','WebSocket','TextEncoder','TextDecoder','atob','btoa','matchMedia','getComputedStyle','alert','confirm','prompt','undefined','NaN','Infinity','parseInt','parseFloat','isNaN','isFinite','encodeURIComponent','decodeURIComponent','structuredClone','AbortController','Notification','crypto','performance','Uint8Array','ArrayBuffer','DataView','Image','HTMLElement','Node','NodeList','Element','DocumentFragment','globalThis','self','top','addEventListener','removeEventListener','scrollTo','open','close','DOMParser','XMLHttpRequest','CSS','Intl','ClipboardItem','requestIdleCallback','reportError','WeakRef','Proxy','Reflect','BigInt','eval','arguments','SVGElement','customElements','IntersectionObserverEntry','CSSStyleSheet','getSelection','visualViewport','screen','devicePixelRatio','innerWidth','innerHeight','scrollX','scrollY','frameElement','parent','postMessage','onerror','onunhandledrejection','import','meta','require','module','exports','process'])
const info={}
for (const label of ['demo','live']) {
  const {text,spans}=layers[label]
  const sf=ts.createSourceFile(label,text,ts.ScriptTarget.ES2022,true,ts.ScriptKind.JS)
  const fileOf=(n)=>{const pos=n.getStart(sf);for(const s of spans)if(pos>=s.start&&pos<s.end)return s.file;return '?'}
  const lineIn=(n)=>{const pos=n.getStart(sf);for(const s of spans)if(pos>=s.start&&pos<s.end){let c=1;for(let i=s.start;i<pos;i++)if(text[i]==='\n')c++;return c}return 0}
  let topNode=sf
  if(label==='live'){let a=null;const f=(n)=>{if(!a&&ts.isArrowFunction(n))a=n;else ts.forEachChild(n,f)};f(sf.statements[0]);topNode=a}
  const topStatements=label==='live'?topNode.body.statements:sf.statements
  const decls=new Set(); const declNodes=new Set()
  for(const st of topStatements){ if(ts.isVariableStatement(st))for(const d of st.declarationList.declarations){const s=new Set();bn(d.name,s);const mark=(nm)=>{if(ts.isIdentifier(nm))declNodes.add(nm);else if(nm.elements)for(const e of nm.elements)e.name&&mark(e.name)};mark(d.name);for(const n of s)decls.add(n)}
    else if((ts.isFunctionDeclaration(st)||ts.isClassDeclaration(st))&&st.name){decls.add(st.name.text);declNodes.add(st.name)} }
  info[label]={sf,fileOf,lineIn,decls,declNodes,topNode,isScope}
}
const allDecls=new Set([...info.demo.decls,...info.live.decls])
const results=[]
for (const label of ['demo','live']) {
  const {sf,fileOf,lineIn,declNodes,topNode}=info[label]
  const cache=new Map(); const scopeNames=(n)=>{if(!cache.has(n))cache.set(n,declaredIn(n));return cache.get(n)}
  const local=(id)=>{for(let p=id.parent;p;p=p.parent){if(p===topNode||(label==='demo'&&ts.isSourceFile(p)))return false;if(isScope(p)&&scopeNames(p).has(id.text))return true}return false}
  const inFn=(id)=>{for(let p=id.parent;p&&p!==topNode;p=p.parent)if(ts.isFunctionLike(p))return true;return false}
  const walk=(n)=>{ if(ts.isIdentifier(n)){const p=n.parent
      const isProp=(ts.isPropertyAccessExpression(p)&&p.name===n)||(ts.isPropertyAssignment(p)&&p.name===n)||(ts.isMethodDeclaration(p)&&p.name===n)||(ts.isBindingElement(p)&&p.propertyName===n)||(ts.isQualifiedName(p)&&p.right===n)||(ts.isGetAccessorDeclaration(p)&&p.name===n)||(ts.isSetAccessorDeclaration(p)&&p.name===n)||(ts.isLabeledStatement(p)&&p.label===n)||ts.isShorthandPropertyAssignment(p)
      const isWriteTarget=(ts.isBinaryExpression(p)&&p.left===n&&p.operatorToken.kind===ts.SyntaxKind.EqualsToken)
      if(!isProp&&!declNodes.has(n)&&!local(n)&&!allDecls.has(n.text)&&!STD.has(n.text)&&!isWriteTarget&&!inFn(n)) results.push({label,file:fileOf(n),line:lineIn(n),name:n.text}) }
    ts.forEachChild(n,walk) }
  walk(sf)
}
console.log('=== eval-time reads of names declared NOWHERE in the legacy layers (i.e. page globals: main.tsx window.X, RavenIslands, etc.) ===')
for(const r of results) console.log(`  ${r.file}:${r.line}  ${r.name}`)
const names=[...new Set(results.map(r=>r.name))].sort()
console.log(`\ndistinct: ${names.length} ${JSON.stringify(names)}`)
