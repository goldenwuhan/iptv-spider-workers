"""webui.py -- HTML pages for the IPTV-Spider web console.

The original app was Jinja2-rendered. Workers has no template engine, so each
page is produced by a small Python function returning an HTML string. All pages
share one inline stylesheet (no CDN, so it renders even when jsdelivr/bootstrap
are unreachable) and talk to the JSON API with vanilla ``fetch``.

Pages (mirroring the original's nav):
    /login /logout     session
    /  /status         dashboard (IP count / program totals / libraries / env)
    /collection        trigger collection + inspect result
    /ip_manager        source servers table (+ multicast view via filter)
    /subscriptions     subscription URLs
    /logs              activity log
    /settings          password + collector options
"""

from __future__ import annotations

from collector import KIND_LABELS
from auth import DEFAULT_PASSWORD

APP_NAME = "IPTV-Spider"
APP_VER = "Workers"

CSS = """
:root{--bg:#f6f7fb;--card:#fff;--line:#e6e8ee;--text:#333;--muted:#8a90a2;
--brand:#007bff;--ok:#28a745;--bad:#dc3545;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-size:14px;
font-family:"Microsoft YaHei",system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:var(--brand);text-decoration:none}
.topbar{display:flex;align-items:center;gap:16px;background:#fff;
border-bottom:1px solid var(--line);padding:0 18px;height:54px;position:sticky;top:0;z-index:20}
.brand{font-size:18px;font-weight:700;color:#222;white-space:nowrap}
.brand .ver{font-size:12px;color:var(--muted);font-weight:400;margin-left:6px}
nav{display:flex;gap:4px;flex-wrap:wrap;flex:1}
nav a{padding:7px 13px;border-radius:6px;color:#555}
nav a:hover{background:#f0f4ff}
nav a.on{background:#e7f0ff;color:var(--brand);font-weight:600}
.logout{color:var(--muted);font-size:13px}
main{max-width:1180px;margin:20px auto;padding:0 16px 60px}
h1{font-size:20px;margin:6px 0 16px}
h2{font-size:16px;margin:0 0 12px}
.grid{display:grid;gap:14px}
.g3{grid-template-columns:repeat(3,1fr)}
.g4{grid-template-columns:repeat(4,1fr)}
@media(max-width:820px){.g3,.g4{grid-template-columns:1fr 1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px}
.stat .k{color:var(--muted);font-size:13px}
.stat .v{font-size:30px;font-weight:700;margin:6px 0 2px}
.stat .s{font-size:12px;color:var(--muted)}
table{width:100%;border-collapse:collapse;background:#fff}
th,td{padding:9px 10px;border-bottom:1px solid var(--line);text-align:left;font-size:13px}
th{background:#fafbfd;color:#555;font-weight:600;white-space:nowrap}
tr:hover td{background:#fcfdff}
.tag{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px}
.tag.new{background:#e8f7ec;color:var(--ok)}
.tag.alive{background:#e6f6fa;color:#17a2b8}
.tag.fail{background:#fdecec;color:var(--bad)}
.tag.unknown{background:#f0f1f5;color:var(--muted)}
.btn{display:inline-block;padding:7px 14px;border-radius:6px;border:1px solid var(--line);
background:#fff;color:#333;cursor:pointer;font-size:13px}
.btn:hover{background:#f7f9fc}
.btn.pri{background:var(--brand);border-color:var(--brand);color:#fff}
.btn.pri:hover{background:#0069d9}
.btn.dan{color:var(--bad);border-color:#f3c9c9}
.btn:disabled{opacity:.55;cursor:default}
input,select{padding:7px 10px;border:1px solid #d6dae3;border-radius:6px;font-size:13px;outline:none}
input:focus,select:focus{border-color:var(--brand)}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.sp{flex:1}
.muted{color:var(--muted)}
.mono{font-family:ui-monospace,Consolas,monospace}
.login-wrap{min-height:100vh;display:flex;align-items:center;justify-content:center}
.login{width:340px;background:#fff;border:1px solid var(--line);border-radius:12px;padding:28px;
box-shadow:0 8px 28px rgba(20,30,60,.06)}
.login h2{margin:0 0 6px;font-size:22px;text-align:center}
.login .sub{text-align:center;color:var(--muted);font-size:12px;margin-bottom:18px}
.login input{width:100%;margin-bottom:12px}
.login .btn{width:100%;padding:10px}
.msg{font-size:13px;margin-top:10px;color:var(--bad);text-align:center;min-height:18px}
.foot{text-align:center;color:var(--muted);font-size:12px;margin-top:26px}
pre{background:#fafbfd;border:1px solid var(--line);border-radius:8px;padding:12px;
overflow:auto;font-size:12px;max-height:340px;margin:0}
"""

NAV = [
    ("/status", "状态"),
    ("/collection", "采集"),
    ("/ip_manager", "IP管理"),
    ("/subscriptions", "订阅"),
    ("/logs", "日志"),
    ("/settings", "设置"),
]


def layout(title: str, active: str, body: str, script: str = "") -> str:
    links = "".join(
        f'<a href="{href}" class="{"on" if href == active else ""}">{name}</a>'
        for href, name in NAV
    )
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{title} - {APP_NAME}</title><style>{CSS}</style></head><body>"
        "<header class=\"topbar\">"
        f"<div class=\"brand\">{APP_NAME}<span class=\"ver\">{APP_VER}</span></div>"
        f"<nav>{links}</nav><a class=\"logout\" href=\"/logout\">退出</a></header>"
        f"<main>{body}</main>"
        + (f"<script>{script}</script>" if script else "")
        + "</body></html>"
    )


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------

def page_login(error: str = "", hint: str = "") -> str:
    warn = ""
    if hint:
        warn = (f'<div class="sub" style="color:#c98a00">首次使用默认密码：'
                f'<b>{DEFAULT_PASSWORD}</b>，登录后请在「设置」中修改</div>')
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>登录 - {APP_NAME}</title><style>{CSS}</style></head><body>"
        "<div class=\"login-wrap\"><form class=\"login\" method=\"post\" action=\"/login\">"
        f"<h2>{APP_NAME}</h2><div class=\"sub\">v2.1.8 · {APP_VER}</div>{warn}"
        "<input type=\"password\" name=\"password\" placeholder=\"请输入密码\" autofocus>"
        "<button class=\"btn pri\" type=\"submit\">登录</button>"
        f"<div class=\"msg\">{error}</div></form></div></body></html>"
    )


# ---------------------------------------------------------------------------
# dashboard
# ---------------------------------------------------------------------------

def page_status() -> str:
    body = """
<h1>状态</h1>
<div class="grid g3" id="stats">
  <div class="card stat"><div class="k">IP 数量</div><div class="v" id="s-ip">-</div><div class="s" id="s-ip-sub">采集到的源服务器</div></div>
  <div class="card stat"><div class="k">节目总量</div><div class="v" id="s-ch">-</div><div class="s">去重后入库存量</div></div>
  <div class="card stat"><div class="k">新上线 IP</div><div class="v" id="s-new">-</div><div class="s">上游标记为「新上线」</div></div>
</div>
<div class="card" style="margin-top:16px">
  <h2>资源库统计</h2>
  <div class="grid g4" id="libs"></div>
</div>
<div class="grid" style="grid-template-columns:1.25fr .75fr;margin-top:16px">
  <div class="card"><h2>最近更新 / 采集历史</h2><div style="max-height:320px;overflow:auto">
    <table><thead><tr><th>时间</th><th>动作</th><th>内容</th></tr></thead><tbody id="logs"></tbody></table>
  </div></div>
  <div class="card"><h2>运行环境与参数</h2>
    <table><tbody id="env"></tbody></table>
    <div class="row" style="margin-top:12px"><a class="btn" href="/settings">前往设置</a></div>
  </div>
</div>
"""
    script = """
const K={hotel:'酒店源',multicast:'组播源',migu:'咪咕源'};
const ST={new:['新上线','new'],alive:['存活','alive'],fail:['失效','fail'],unknown:['未知','unknown']};
function tag(s){const x=ST[s]||ST.unknown;return '<span class="tag '+x[1]+'">'+x[0]+'</span>';}
async function load(){
  const r=await (await fetch('/api/stats')).json();
  document.getElementById('s-ip').textContent=r.ip_count;
  document.getElementById('s-ch').textContent=r.channel_count;
  document.getElementById('s-new').textContent=r.new_count;
  document.getElementById('s-ip-sub').textContent='共 '+r.source_count+' 个源服务器';
  document.getElementById('libs').innerHTML=Object.keys(K).map(k=>{
    const d=r.libs[k]||{count:0,programs:0,channels:0};
    return '<div><div class="k muted">'+K[k]+'</div><div class="v" style="font-size:22px;font-weight:700">'+d.count+' 节点</div><div class="s">'+d.programs+' 节目 / 已入库 '+d.channels+'</div></div>';
  }).join('');
  document.getElementById('env').innerHTML=[
    ['程序版本','v2.1.8 · Workers'],
    ['运行模式','Cloudflare Workers'],
    ['系统当前时间',new Date().toLocaleString('zh-CN')],
    ['已入库频道',r.channel_count],
    ['源服务器总数',r.source_count],
    ['分组数量',r.group_count],
    ['自动采集',r.auto_collect?'已开启':'已关闭'],
    ['采集周期',r.collect_cron||'—'],
  ].map(x=>'<tr><td class="muted">'+x[0]+'</td><td><b>'+x[1]+'</b></td></tr>').join('');
  const logs=await (await fetch('/api/logs?limit=15')).json();
  document.getElementById('logs').innerHTML=logs.map(l=>'<tr><td class="mono">'+new Date(l.ts*1000).toLocaleString('zh-CN')+'</td><td>'+l.action+'</td><td>'+(l.message||'')+'</td></tr>').join('')||'<tr><td colspan="3" class="muted">暂无记录</td></tr>';
}
load();
"""
    return layout("状态", "/status", body, script)


# ---------------------------------------------------------------------------
# collection
# ---------------------------------------------------------------------------

def page_collection() -> str:
    opts = "".join(f'<option value="{k}">{v}</option>' for k, v in KIND_LABELS.items())
    body = f"""
<h1>采集</h1>
<div class="card">
  <h2>手动采集</h2>
  <div class="row">
    <label>来源</label><select id="kind">{opts}<option value="all">全部（三类依次）</option></select>
    <label>页数</label><input id="pages" type="number" value="1" min="1" max="10" style="width:70px">
    <label>每类最多处理</label><input id="max" type="number" value="12" min="1" max="40" style="width:70px">
    <button class="btn pri" id="go">开始采集</button>
    <span class="sp"></span><span class="muted" id="tip"></span>
  </div>
  <div class="muted" style="margin-top:10px;font-size:12px">
    采集自 <span class="mono">api.cqshushu.com</span>（酒店源 / 组播源 / 咪咕源）。
    单次请求能发起的子请求有限，源较多时请多跑几次或调小「每类最多处理」。
  </div>
  <pre id="out" style="margin-top:14px;display:none"></pre>
</div>
<div class="card" style="margin-top:16px">
  <h2>已采集的源</h2>
  <div class="row" style="margin-bottom:10px">
    <select id="fkind"><option value="">全部类型</option>{opts}</select>
    <button class="btn" id="refresh">刷新</button>
  </div>
  <div style="max-height:420px;overflow:auto">
  <table><thead><tr><th>类型</th><th>名称</th><th>IP:端口</th><th>节目数</th><th>已入库</th><th>状态</th></tr></thead>
  <tbody id="rows"></tbody></table></div>
</div>
"""
    script = """
const K={hotel:'酒店源',multicast:'组播源',migu:'咪咕源'};
const ST={new:['新上线','new'],alive:['存活','alive'],fail:['失效','fail'],unknown:['未知','unknown']};
function tag(s){const x=ST[s]||ST.unknown;return '<span class="tag '+x[1]+'">'+x[0]+'</span>';}
async function loadRows(){
  const k=document.getElementById('fkind').value;
  const rows=await (await fetch('/api/sources'+(k?('?kind='+k):''))).json();
  document.getElementById('rows').innerHTML=rows.map(s=>
    '<tr><td>'+(K[s.kind]||s.kind)+'</td><td>'+(s.name||'-')+'</td><td class="mono">'+s.addr+
    '</td><td>'+s.program_count+'</td><td>'+s.channel_count+'</td><td>'+tag(s.up_status)+'</td></tr>'
  ).join('')||'<tr><td colspan="6" class="muted">暂无数据，先点「开始采集」</td></tr>';
}
document.getElementById('refresh').onclick=loadRows;
document.getElementById('fkind').onchange=loadRows;
document.getElementById('go').onclick=async()=>{
  const btn=document.getElementById('go'),out=document.getElementById('out');
  btn.disabled=true;document.getElementById('tip').textContent='采集中，请稍候…';
  out.style.display='block';out.textContent='请求中…';
  try{
    const body={kind:document.getElementById('kind').value,
      pages:+document.getElementById('pages').value,
      max_sources:+document.getElementById('max').value};
    const res=await fetch('/api/collect',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const data=await res.json();
    out.textContent=JSON.stringify(data,null,2);
    document.getElementById('tip').textContent='完成';
  }catch(e){out.textContent='失败: '+e;document.getElementById('tip').textContent='失败';}
  btn.disabled=false;loadRows();
};
loadRows();
"""
    return layout("采集", "/collection", body, script)


# ---------------------------------------------------------------------------
# ip manager
# ---------------------------------------------------------------------------

def page_ip_manager() -> str:
    opts = "".join(f'<option value="{k}">{v}</option>' for k, v in KIND_LABELS.items())
    body = f"""
<h1>IP 管理</h1>
<div class="card">
  <div class="row" style="margin-bottom:12px">
    <select id="fkind"><option value="">全部类型</option>{opts}</select>
    <select id="fstat"><option value="">全部状态</option>
      <option value="new">新上线</option><option value="alive">存活</option>
      <option value="fail">失效</option><option value="unknown">未知</option></select>
    <input id="q" placeholder="搜索名称 / IP" style="width:180px">
    <button class="btn" id="refresh">刷新</button>
    <span class="sp"></span><span class="muted" id="cnt"></span>
  </div>
  <div style="max-height:560px;overflow:auto">
  <table><thead><tr><th>类型</th><th>名称</th><th>IP:端口</th><th>节目数</th><th>已入库</th>
  <th>状态</th><th>更新时间</th><th>操作</th></tr></thead><tbody id="rows"></tbody></table></div>
</div>
"""
    script = """
const K={hotel:'酒店源',multicast:'组播源',migu:'咪咕源'};
const ST={new:['新上线','new'],alive:['存活','alive'],fail:['失效','fail'],unknown:['未知','unknown']};
function tag(s){const x=ST[s]||ST.unknown;return '<span class="tag '+x[1]+'">'+x[0]+'</span>';}
let CACHE=[];
async function load(){
  const k=document.getElementById('fkind').value;
  CACHE=await (await fetch('/api/sources'+(k?('?kind='+k):''))).json();
  render();
}
function render(){
  const st=document.getElementById('fstat').value,q=document.getElementById('q').value.trim().toLowerCase();
  const rows=CACHE.filter(s=>(!st||s.up_status===st)&&(!q||(s.name||'').toLowerCase().includes(q)||s.addr.includes(q)));
  document.getElementById('cnt').textContent='共 '+rows.length+' 条';
  document.getElementById('rows').innerHTML=rows.map(s=>
    '<tr><td>'+(K[s.kind]||s.kind)+'</td><td>'+(s.name||'-')+'</td><td class="mono">'+s.addr+
    '</td><td>'+s.program_count+'</td><td>'+s.channel_count+'</td><td>'+tag(s.up_status)+
    '</td><td class="muted">'+(s.update_time||'-')+'</td>'+
    '<td><button class="btn dan" data-del="'+s.id+'">删除</button></td></tr>'
  ).join('')||'<tr><td colspan="8" class="muted">暂无数据</td></tr>';
}

document.getElementById('refresh').onclick=load;
document.getElementById('fkind').onchange=load;
document.getElementById('fstat').onchange=render;
document.getElementById('q').oninput=render;
document.addEventListener('click',async e=>{
  const id=e.target.getAttribute&&e.target.getAttribute('data-del');
  if(!id)return;
  if(!confirm('确定删除该源服务器？'))return;
  await fetch('/api/sources/'+id,{method:'DELETE'});
  load();
});
load();
"""
    return layout("IP 管理", "/ip_manager", body, script)


# ---------------------------------------------------------------------------
# subscriptions
# ---------------------------------------------------------------------------

def page_subscriptions() -> str:
    body = """
<style>
.modal{position:fixed;inset:0;background:rgba(20,30,60,.35);display:flex;
align-items:flex-start;justify-content:center;z-index:50;padding:40px 16px;overflow:auto}
.modal-box{background:#fff;border-radius:10px;width:520px;max-width:100%;
box-shadow:0 12px 40px rgba(20,30,60,.18)}
.modal-head{display:flex;align-items:center;justify-content:space-between;
padding:14px 18px;border-bottom:1px solid var(--line);font-size:15px}
.modal-head .x{cursor:pointer;color:var(--muted);font-size:16px}
.modal-body{padding:16px 18px}
.modal-foot{display:flex;justify-content:flex-end;gap:10px;padding:12px 18px;
border-top:1px solid var(--line)}
.lb{display:block;margin:14px 0 8px;font-size:13px;color:#555}
.lb.first{margin-top:0}
.chks{display:grid;grid-template-columns:repeat(5,1fr);gap:6px 4px;
border:1px solid var(--line);border-radius:8px;padding:10px}
.chks label{display:flex;align-items:center;gap:5px;font-size:13px;color:#444;cursor:pointer}
.tagk{display:inline-block;padding:1px 7px;border-radius:9px;font-size:12px;margin:1px 3px 1px 0}
.tagk.hotel{background:#e7f0ff;color:#007bff}
.tagk.migu{background:#e8f7ec;color:#28a745}
.tagk.multicast{background:#fff4e5;color:#c98a00}
.tagk.gray{background:#f0f1f5;color:#8a90a2}
.act{white-space:nowrap}
</style>
<h1 style="display:flex;align-items:center">
  <span>订阅管理</span><span class="sp"></span>
  <button class="btn pri" id="add">＋ 新增订阅</button>
</h1>
<div class="card">
  <div style="overflow:auto">
  <table><thead><tr>
    <th>ID</th><th>类型</th><th>省份</th><th>运营商</th>
    <th>最小速度 / 分辨率</th><th>生成文件</th><th>创建时间</th><th>操作</th>
  </tr></thead><tbody id="rows"></tbody></table></div>
</div>

<div id="modal" class="modal" style="display:none">
  <div class="modal-box">
    <div class="modal-head"><b>创建订阅</b><span class="x" id="close">✕</span></div>
    <div class="modal-body">
      <label class="lb first">Token（8位随机，也可自定义）</label>
      <div class="row"><input id="f-token" style="flex:1"><button class="btn" id="gen">生成</button></div>
      <label class="lb">筛选类型（不选则全选）</label><div class="chks" id="f-kinds"></div>
      <label class="lb">筛选省份（不选则全选）</label><div class="chks" id="f-provs"></div>
      <label class="lb">筛选运营商（不选则全选）</label><div class="chks" id="f-isps"></div>
      <label class="lb">最低速度要求（MB/s）</label>
      <input id="f-speed" type="number" value="0" step="0.1" style="width:100%">
      <div class="muted" style="font-size:12px;margin-top:6px">
        订阅链接将只包含测速值大于或等于此数值的 IP<br>
        （Cloudflare Workers 版无 ffprobe 测速，此项暂不生效）
      </div>
    </div>
    <div class="modal-foot">
      <button class="btn" id="cancel">取消</button>
      <button class="btn pri" id="save">保存</button>
    </div>
  </div>
</div>
"""
    script = """
function csv(v){return (v||'').split(',').filter(Boolean);}
function tags(vals,cls){
  const a=csv(vals);
  if(!a.length)return '<span class="tagk gray">全部</span>';
  return a.map(v=>'<span class="tagk '+(cls||'gray')+'">'+v+'</span>').join('');
}
function fmt(ts){return ts?new Date(ts*1000).toLocaleString('zh-CN'):'-';}

async function load(){
  const rows=await (await fetch('/api/subscriptions')).json();
  document.getElementById('rows').innerHTML=rows.map(s=>
    '<tr><td class="mono">'+s.token+'</td>'+
    '<td>'+tags(s.kinds,'hotel')+'</td>'+
    '<td>'+tags(s.provinces,'gray')+'</td>'+
    '<td>'+tags(s.isps,'gray')+'</td>'+
    '<td>≥'+(s.min_speed||0)+' MB/s'+(s.min_res?(' / ≥'+s.min_res+'P'):'')+'</td>'+
    '<td>'+(s.count>0?('是 ('+s.count+')'):'否')+'</td>'+
    '<td class="muted">'+fmt(s.created_at)+'</td>'+
    '<td class="act">'+
      '<a class="btn" href="'+s.txt_url+'" target="_blank">TXT</a>'+
      '<a class="btn" href="'+s.m3u_url+'" target="_blank">M3U</a>'+
      '<button class="btn" data-edit="'+s.token+'">编辑</button>'+
      '<button class="btn dan" data-del="'+s.token+'">删除</button>'+
    '</td></tr>'
  ).join('')||'<tr><td colspan="8" class="muted">暂无订阅，点右上角「新增订阅」</td></tr>';
}

function box(id, values, checked){
  document.getElementById(id).innerHTML=values.map(v=>
    '<label><input type="checkbox" value="'+v+'"'+(checked.includes(v)?' checked':'')+'>'+v+'</label>'
  ).join('')||'<span class="muted">（暂无数据，请先采集）</span>';
}
function checkedIn(id){
  return [...document.querySelectorAll('#'+id+' input:checked')].map(i=>i.value);
}
function rnd(n){
  const a='ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  const r=crypto.getRandomValues(new Uint8Array(n));let s='';
  for(let i=0;i<n;i++)s+=a[r[i]%a.length];return s;
}

async function openModal(sub){
  const meta=await (await fetch('/api/subscriptions/meta')).json();
  const k=sub?csv(sub.kinds):[],p=sub?csv(sub.provinces):[],i=sub?csv(sub.isps):[];
  box('f-kinds', meta.kinds, k);
  box('f-provs', meta.provinces, p);
  box('f-isps', meta.isps, i);
  document.getElementById('f-token').value=sub?sub.token:rnd(8);
  document.getElementById('f-speed').value=sub?(sub.min_speed||0):0;
  document.getElementById('modal').style.display='flex';
}
function closeModal(){document.getElementById('modal').style.display='none';}

document.getElementById('add').onclick=()=>openModal(null);
document.getElementById('close').onclick=closeModal;
document.getElementById('cancel').onclick=closeModal;
document.getElementById('gen').onclick=()=>{document.getElementById('f-token').value=rnd(8);};
document.getElementById('save').onclick=async()=>{
  const body={token:document.getElementById('f-token').value.trim(),
    kinds:checkedIn('f-kinds'), provinces:checkedIn('f-provs'),
    isps:checkedIn('f-isps'),
    min_speed:+document.getElementById('f-speed').value||0, min_res:0};
  const r=await fetch('/api/subscriptions',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(!r.ok){alert((await r.json()).error||'保存失败');return;}
  closeModal();load();
};
document.addEventListener('click',async e=>{
  const t=e.target, del=t.getAttribute&&t.getAttribute('data-del'),
        ed=t.getAttribute&&t.getAttribute('data-edit');
  if(del){ if(!confirm('删除订阅 '+del+'？'))return;
    await fetch('/api/subscriptions/'+del,{method:'DELETE'}); load(); }
  if(ed){ const rows=await (await fetch('/api/subscriptions')).json();
    openModal(rows.find(x=>x.token===ed)); }
});
load();
"""
    return layout("订阅", "/subscriptions", body, script)


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------

def page_logs() -> str:
    body = """
<h1>日志</h1>
<div class="card">
  <div class="row" style="margin-bottom:12px">
    <select id="lvl"><option value="">全部级别</option><option value="info">info</option>
      <option value="warn">warn</option><option value="error">error</option></select>
    <button class="btn" id="refresh">刷新</button>
    <button class="btn dan" id="clear">清空日志</button>
    <span class="sp"></span><span class="muted" id="cnt"></span>
  </div>
  <div style="max-height:620px;overflow:auto">
  <table><thead><tr><th style="width:170px">时间</th><th style="width:70px">级别</th>
  <th style="width:110px">动作</th><th>内容</th></tr></thead><tbody id="rows"></tbody></table></div>
</div>
"""
    script = """
let DATA=[];
const LV={info:'',warn:'#c98a00',error:'#dc3545'};
async function load(){
  DATA=await (await fetch('/api/logs?limit=400')).json();
  render();
}
function render(){
  const lv=document.getElementById('lvl').value;
  const rows=DATA.filter(l=>!lv||l.level===lv);
  document.getElementById('cnt').textContent='共 '+rows.length+' 条';
  document.getElementById('rows').innerHTML=rows.map(l=>
    '<tr><td class="mono muted">'+new Date(l.ts*1000).toLocaleString('zh-CN')+
    '</td><td style="color:'+(LV[l.level]||'')+'">'+l.level+'</td><td>'+l.action+
    '</td><td>'+(l.message||'')+'</td></tr>'
  ).join('')||'<tr><td colspan="4" class="muted">暂无日志</td></tr>';
}
document.getElementById('refresh').onclick=load;
document.getElementById('lvl').onchange=render;
document.getElementById('clear').onclick=async()=>{
  if(!confirm('确定清空全部日志？'))return;
  await fetch('/api/logs',{method:'DELETE'});load();
};
load();
"""
    return layout("日志", "/logs", body, script)


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

def page_settings() -> str:
    opts = "".join(f'<option value="{k}">{v}</option>' for k, v in KIND_LABELS.items())
    body = f"""
<h1>设置</h1>
<div class="card">
  <h2>修改密码</h2>
  <div class="row">
    <input id="pwd" type="password" placeholder="新密码" style="width:200px">
    <button class="btn pri" id="savePwd">保存密码</button>
    <span class="sp"></span><span class="muted" id="pwdTip"></span>
  </div>
  <div class="muted" style="margin-top:8px;font-size:12px">
    若在 Worker 里配置了环境变量 <span class="mono">ADMIN_PASSWORD</span>，它的优先级高于此处保存的密码。
  </div>
</div>

<div class="card" style="margin-top:16px">
  <h2>采集参数</h2>
  <div class="row" style="margin-bottom:10px">
    <label>采集来源</label><select id="kinds" multiple size="3" style="min-width:130px">
      {opts}</select>
    <label>每类页数</label><input id="pages" type="number" min="1" max="10" style="width:70px">
    <label>每类最多处理</label><input id="max" type="number" min="1" max="40" style="width:70px">
  </div>
  <div class="row">
    <label>自动采集</label><select id="auto"><option value="1">开启</option><option value="0">关闭</option></select>
    <span class="muted" style="font-size:12px">开启后由 Cron Trigger 定时执行（见 wrangler.toml 的 crons）</span>
  </div>
  <div class="row" style="margin-top:14px">
    <button class="btn pri" id="save">保存设置</button><span class="muted" id="tip"></span>
  </div>
</div>

<div class="card" style="margin-top:16px">
  <h2>数据维护</h2>
  <div class="row">
    <button class="btn dan" id="clearCh">清空全部频道</button>
    <span class="muted" style="font-size:12px">仅清空频道，不影响源服务器记录</span>
  </div>
</div>
<div class="foot">{APP_NAME} v2.1.8 · Cloudflare Workers 版 · Power by cqshushu</div>
"""
    script = """
async function load(){
  const s=await (await fetch('/api/settings')).json();
  document.getElementById('pages').value=s.collect_pages||1;
  document.getElementById('max').value=s.collect_max||12;
  document.getElementById('auto').value=s.auto_collect?'1':'0';
  const kinds=(s.collect_kinds||'hotel,multicast,migu').split(',');
  [...document.getElementById('kinds').options].forEach(o=>o.selected=kinds.includes(o.value));
}
document.getElementById('savePwd').onclick=async()=>{
  const p=document.getElementById('pwd').value;
  if(!p){document.getElementById('pwdTip').textContent='请输入新密码';return;}
  await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({admin_password:p})});
  document.getElementById('pwdTip').textContent='已保存';document.getElementById('pwd').value='';
};
document.getElementById('save').onclick=async()=>{
  const kinds=[...document.getElementById('kinds').selectedOptions].map(o=>o.value);
  await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({collect_kinds:kinds.join(','),
      collect_pages:+document.getElementById('pages').value,
      collect_max:+document.getElementById('max').value,
      auto_collect:document.getElementById('auto').value==='1'})});
  document.getElementById('tip').textContent='已保存';
};
document.getElementById('clearCh').onclick=async()=>{
  if(!confirm('确定清空全部频道？此操作不可撤销。'))return;
  await fetch('/api/channels',{method:'DELETE'});
  alert('已清空');
};
load();
"""
    return layout("设置", "/settings", body, script)
