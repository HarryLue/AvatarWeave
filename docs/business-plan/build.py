"""Render the editable business plan and reproduce assumption-only financial CSVs.

Run from repo root: backend/.venv/bin/python docs/business-plan/build.py
Then: node docs/business-plan/render-pdf.mjs
"""
import csv
from pathlib import Path
from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parent
body = MarkdownIt().enable('table').render((ROOT / 'README.md').read_text())
css = '''
:root{--ink:#203e40;--mint:#88b7aa;--paper:#fffaf3;--coral:#ef957d}
*{box-sizing:border-box}body{margin:0;background:#e9eeeb;color:var(--ink);font:15px/1.85 'Noto Sans CJK SC','Microsoft YaHei','PingFang SC',sans-serif}
main{max-width:960px;margin:32px auto;background:var(--paper);padding:65px 72px;box-shadow:0 12px 45px #203e4015}
h1{font-size:39px;line-height:1.3;letter-spacing:-1px;margin:12px 0 22px}h2{font-size:25px;line-height:1.45;margin:55px 0 23px;padding-top:14px;border-top:3px solid var(--mint)}
p{margin:14px 0}strong{color:#174e4c}a{color:#256d68;text-decoration:none;overflow-wrap:anywhere}code{font-size:.82em;overflow-wrap:anywhere;background:#e9efea;padding:1px 4px}
table{border-collapse:collapse;width:100%;font-size:12px;line-height:1.65;margin:22px 0;table-layout:auto}th{background:#24494a;color:white;text-align:left}th,td{padding:10px 11px;border-bottom:1px solid #cbd8d0;vertical-align:top}tr:nth-child(even) td{background:#eef2eb}
img{max-width:100%;height:auto;border-radius:8px}blockquote{border-left:4px solid var(--coral);margin:20px 0;padding:2px 20px}.cover{margin:0 0 40px}.eyebrow{font-size:12px;letter-spacing:3px;color:#557b72}.cover-title{font-size:52px;font-weight:700;line-height:1.25;margin:28px 0}.cover-subtitle{font-size:21px;line-height:1.6}.cover img{margin:30px 0 20px}.cover-note{font-size:12px;color:#6c7e76}.pill{display:inline-block;border:1px solid #9bb4a8;border-radius:20px;padding:3px 14px;font-size:12px;margin:12px 6px 0 0}
@media(max-width:650px){main{margin:0;padding:28px 20px}h1{font-size:29px}h2{font-size:23px}.cover-title{font-size:40px}table{font-size:11px}td,th{padding:7px 5px}}
@page{size:A4;margin:17mm 17mm 19mm}
@media print{body{background:white;font-size:10pt;line-height:1.65;-webkit-print-color-adjust:exact;print-color-adjust:exact}main{margin:0;padding:0;max-width:none;box-shadow:none;background:white}h1{font-size:27pt}h2{font-size:19pt;break-before:page;margin:0 0 6mm;padding-top:4mm;break-after:avoid}p{margin:3mm 0;orphans:3;widows:3}table{font-size:8.1pt;margin:4mm 0;line-height:1.6}th,td{padding:2.4mm 2mm}tr{break-inside:avoid}thead{display:table-header-group}img{break-inside:avoid}.cover{break-after:page;margin:0;padding-top:16mm}.cover-title{font-size:40pt;margin:11mm 0}.cover-subtitle{font-size:17pt}.cover img{margin:14mm 0 9mm}.cover-note{font-size:10pt}.eyebrow{font-size:10pt}.pill{font-size:10pt}a{color:#256d68}h1+p{font-size:9pt;color:#61736c}}
'''
cover = '''<section class="cover"><div class="eyebrow">AVATARWEAVE / BUSINESS PLAN / 2026</div><div class="cover-title">织伴<br>卡通 AI 生活伙伴</div><div class="cover-subtitle">语音建立日常连接<br>可信记忆延续共同经历</div><img src="assets/xiaozhi-character-sheet.png" alt="小织原创角色设定图"><div><span class="pill">语音优先</span><span class="pill">可信记忆</span><span class="pill">卡通表达</span></div><p class="cover-note">创始人决策与种子轮沟通稿 · 2026 年 11 月 5 日<br>工作名及商业假设待验证 · 工程原型阶段</p></section>'''
(ROOT / 'business-plan.html').write_text('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>AvatarWeave 织伴商业计划书</title><style>'+css+'</style></head><body><main>'+cover+body+'</main></body></html>')

price, channel, minutes, minute_cost, support = 59, .05, 180, .08, 3
contribution = price*(1-channel)-minutes*minute_cost-support
with (ROOT/'financial-scenarios.csv').open('w',newline='',encoding='utf-8-sig') as f:
    w=csv.writer(f)
    w.writerow(['情景_全部为假设','月活跃用户','月付费用户','单价元','分钟每付费用户','成本每分钟元','渠道退款率','其他变动成本每人元','月流水元','付费用户贡献元','固定开支元','营销元','免费服务预算元','税前一次性支出前结余元'])
    for name,active,paid,marketing,free in [('保守',10000,300,10000,5000),('基准',50000,2500,40000,20000),('增长',150000,10000,120000,50000)]:
        total=round(paid*contribution,2)
        w.writerow([name,active,paid,price,minutes,minute_cost,channel,support,paid*price,total,150000,marketing,free,round(total-150000-marketing-free,2)])
with (ROOT/'first-year-cohorts.csv').open('w',newline='',encoding='utf-8-sig') as f:
    w=csv.writer(f);w.writerow(['月_假设','新增付费','月流失率','月在订用户_期望值','流水元_当月新增计整月'])
    users=0
    for month in range(1,13):
        users=users*.92+1600/12
        w.writerow([month,round(1600/12,4),.08,round(users,4),round(users*59,2)])
print('Rendered business-plan.html and two financial CSVs.')
