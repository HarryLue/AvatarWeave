const fs=require('fs'),path=require('path');
const deps=process.env.BP_NODE_MODULES||'/tmp/avatarweave-bp-v2-tools/node_modules';
const pptxgen=require(path.join(deps,'pptxgenjs'));
const root=__dirname, pptx=new pptxgen();pptx.layout='LAYOUT_WIDE';pptx.author='路伟 | AvatarWeave';pptx.subject='未来个人 AI 入口：融资商业计划书 V2';pptx.title='AvatarWeave 融资商业计划书 V2';pptx.company='AvatarWeave';pptx.lang='zh-CN';pptx.theme={headFontFace:'Microsoft YaHei',bodyFontFace:'Microsoft YaHei',lang:'zh-CN'};
const C={bg:'F5F5EE',ink:'173C3D',muted:'607672',mint:'A8D6BD',coral:'ED9B80',light:'E6EDE4',white:'FFFFFF',dark:'102F32'};
const sources={
 S1:{name:'CNNIC 第57次报告',date:'2026-02-05发布；统计截至2025-12',url:'https://www.cnnic.org.cn/n4/2026/0304/c88-11549.html',claim:'中国生成式AI用户6.02亿，普及率42.8%。总体用户，非陪伴付费市场。'},
 S2:{name:'Sensor Tower State of AI Apps 2025',date:'2025-08发布；统计2025年上半年',url:'https://sensortower.com/blog/state-of-ai-apps-market-overview-2025',claim:'全球生成式AI应用IAP收入接近19亿美元。机构估算；包括助手及内容生成，不等于陪伴市场，不含所有渠道收入。'},
 S3:{name:'Stanford HAI AI Index 2025',date:'2022-11至2024-10对比',url:'https://hai.stanford.edu/ai-index/2025-ai-index-report/research-and-development',claim:'达到GPT-3.5同等MMLU水平的模型调用价格由20美元降至0.07美元/百万tokens，超过280倍下降；非端到端语音成本。'},
 S4:{name:'USV Tolan投资公告',date:'2026-10-19',url:'https://www.usv.com/writing/tolan',claim:'领投3000万美元；公告预计当月超过25万人使用，过去一年超过7000万次对话；描述购物、生日与家庭协作。投资方披露，不是独立审计。'},
 S5:{name:'Google Connected Apps Platform',date:'查阅2026-11-05；平台预览阶段',url:'https://developers.google.com/connected-apps/',claim:'通过授权第三方数据与工具，将服务接入Gemini和Search中的对话。仅证明行业方向，非本项目已接入。'},
 S6:{name:'Character.AI Calls & Voice FAQ',date:'页面标注更新2024-08-23；查阅2026-11-05',url:'https://support.character.ai/hc/en-us/articles/23957274129691-Character-Calls-Voice-FAQ',claim:'支持与角色双向语音交流及文字回看。不能宣称角色语音本身独家。'},
 S7:{name:'Portola 创始人 Introducing Tolan',date:'2025-02-25',url:'https://www.tolans.com/relay/introducing-tolan',claim:'披露1000万美元融资；招聘工程、动画、设计和写作人才；提出面向不同人生阶段的共同引擎。未披露预算占比。'},
 S8:{name:'GeekWire Tolan融资报道',date:'2025-07-07',url:'https://www.geekwire.com/2025/ai-companionship-app-tolan-raises-20m-to-help-more-people-grow-with-a-virtual-alien-friend/',claim:'报道2000万美元A轮，12名员工及工程招聘；创始人解释免费版本扩展需要推理成本资金。历史报道，非当前员工数。'},
 P1:{name:'项目开发任务与交付记录',date:'2026-11-05',url:'../../voice-memory-development/tasks.md',claim:'开发环境语音、记忆、纠正、删除与恢复已有实现和验证记录。未完成生产联合验收。'},
 F1:{name:'创始人本次提供',date:'2026-11-05',url:'',claim:'AvatarWeave；中国用户；尚未商业化；路伟，硕士，全职独立开发，8年算法研发经验，好未来、爱学习经历；时间序列预测、量化交易、情感陪伴数字人项目。未经第三方背调。'}
};
let all=[],cur,s,n=0;
function esc(x){return String(x).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;')}
function shape(x,y,w,h,fill,line=fill,r=0){s.addShape(r?pptx.ShapeType.roundRect:pptx.ShapeType.rect,{x,y,w,h,rectRadius:r, radius:r,fill:{color:fill},line:{color:line,width:.6},radius:r});cur.html.push(`<div style="position:absolute;left:${x*96}px;top:${y*96}px;width:${w*96}px;height:${h*96}px;background:#${fill};border:1px solid #${line};border-radius:${r?14:0}px"></div>`)}
function text(t,x,y,w,h,size=20,color=C.ink,bold=false,align='left'){s.addText(t,{x,y,w,h,fontFace:'Microsoft YaHei',fontSize:size,color,bold,margin:0,breakLine:false,align,valign:'top',paraSpaceAfterPt:0,lineSpacingMultiple:1.14,lang:'zh-CN'});cur.html.push(`<div class="txt" data-text="${esc(t)}" style="position:absolute;left:${x*96}px;top:${y*96}px;width:${w*96}px;height:${h*96}px;font-size:${size*96/72}px;color:#${color};font-weight:${bold?700:400};text-align:${align};line-height:1.20;white-space:pre-wrap">${esc(t)}</div>`);cur.texts.push(t)}
function image(file,x,y,w,h){s.addImage({path:path.resolve(root,file),x,y,w,h});cur.html.push(`<img style="position:absolute;left:${x*96}px;top:${y*96}px;width:${w*96}px;height:${h*96}px;object-fit:fill" src="${file}">`)}
function line(x,y,w,color=C.mint){shape(x,y,w,.025,color)}
function begin(k,title,sub='',refs=[],dark=false){s=pptx.addSlide();s.background={color:dark?C.dark:C.bg};cur={id:++n,k,title,sub,refs,html:[],texts:[],notes:'',dark};all.push(cur);text('AVATARWEAVE  /  '+k,.55,.28,11,.25,9,dark?C.mint:C.muted,true);text(title,.55,.86,12.2,.85,30,dark?C.white:C.ink,true);if(sub)text(sub,.57,1.75,12.1,.55,13,dark?C.mint:C.muted);return s}
function end(notes){cur.notes=notes;s.addNotes(notes+'\n\n依据：\n'+cur.refs.map(id=>`${id} ${sources[id].name} | ${sources[id].date}\n${sources[id].url}\n${sources[id].claim}`).join('\n\n'));line(.55,6.96,12.22,cur.dark?'38585A':'CDD8CE');text('AVATARWEAVE  |  INVESTOR PRESENTATION  |  V2  |  2026.11',.55,7.10,10,.18,7,cur.dark?C.mint:C.muted);text(String(n).padStart(2,'0'),12.05,7.07,.7,.25,9,cur.dark?C.white:C.ink,false,'right');if(cur.refs.length)text(cur.refs.map(id=>id+' '+sources[id].name+' · '+sources[id].date).join('  /  '),.58,6.52,12.05,.38,7.3,cur.dark?C.mint:C.muted);}
function card(x,y,w,h,k,t,body,dark=false){shape(x,y,w,h,dark?'214346':C.light, dark?'214346':C.light,1);text(k,x+.22,y+.22,w-.44,.3,10,dark?C.mint:C.muted,true);text(t,x+.22,y+.78,w-.44,.65,21,dark?C.white:C.ink,true);text(body,x+.22,y+1.58,w-.44,h-1.73,15,dark?C.mint:C.muted)}
function band(t,y=5.85,dark=false){shape(.57,y,12.18,.49,dark?'284B4E':'DDE9DC');text(t,.78,y+.10,11.75,.32,14,dark?C.white:C.ink,true)}

// 01
begin('VISION','', '',[],true);image('assets/future-entry-vision.png',0,0,13.333,7.5);text('AVATARWEAVE',.65,.52,7,.4,16,C.mint,true);text('未来的个人\nAI 入口',.65,1.65,6.1,1.75,43,C.white,true);text('从愿意开口的卡通伙伴\n到理解你、连接服务的个人智能体',.70,3.85,5.4,1,20,C.mint);text('融资商业计划书  V2\n中国市场  ·  创始人 路伟',.70,5.62,5,.7,13,C.white);text('产品愿景图 · 非现有界面',9.35,6.65,3.2,.3,9,C.mint,false,'right');end('AvatarWeave希望成为未来的个人AI入口。切入点是一个能自然交流并记住共同经历的卡通伙伴，长期目标是让用户通过同一个可信身份获得个性化服务。我们从中国用户起步，当前已有工程原型，尚未商业化。');
// 02
begin('INVESTMENT THESIS','入口的价值，来自持续理解一个人','语音降低表达门槛，长期记忆积累上下文，授权行动把理解转化为价值。');
card(.6,2.55,3.85,2.65,'01  日常连接','愿意开口','用卡通角色与自然语音\n建立可持续的交流习惯');card(4.74,2.55,3.85,2.65,'02  持续理解','无需重讲背景','围绕人物、偏好和经历\n形成用户可控的个人上下文');card(8.88,2.55,3.85,2.65,'03  生活服务','得到具体结果','识别真实意图，经用户确认\n调用服务并追踪结果');band('战略目标：成为用户表达生活意图时，愿意首先打开的产品。');end('这是投资判断而非已证明的市场规律。单次语音交互容易被复制，持续理解用户并可靠完成生活事务才可能产生长期价值。我们的先后顺序是建立关系、证明记忆的增量价值，再扩大服务范围。');
//03
begin('PROBLEM','用户的生活是连续的，服务却是分散的','以一次搬家为例：情绪、偏好、约定和待办，其实属于同一个人的同一段生活。');
text('用户真正想表达',.65,2.55,4.7,.5,19,C.muted);text('“搬家快到了，\n我还有好多事没理清。”',.65,3.22,5.4,1.1,26,C.ink,true);text('场景示意；非访谈引语',.68,4.65,5,.3,10,C.muted);
card(6.55,2.55,6.15,2.95,'当前体验的结构性摩擦','用户自己承担衔接','倾诉时重新解释背景\n安排时切换多个工具\n完成后还要自己更新进展');band('机会：围绕“这个人正在经历什么”，组织交流、记忆和行动。');end('这里用搬家说明产品要解决的具体问题，不把示意场景当成真实用户访谈。我们要降低的是重复解释背景、跨应用组织事情、追踪变化的负担。下一阶段需要用真实用户访谈确认痛点强度及愿付费的部分。');
//04
begin('WHY NOW','使用习惯、付费行为与模型经济性正在改变','这些外部事实为个人 AI 产品提供基础；它们不等于 AvatarWeave 已取得市场验证。',['S1','S2','S3']);
card(.6,2.50,3.85,2.85,'中国 · 截至2025年12月','6.02 亿','生成式 AI 用户\nCNNIC 统计');card(4.74,2.50,3.85,2.85,'全球 · 2025年上半年','近 19 亿美元','生成式 AI 应用内购收入\nSensor Tower 估算');card(8.88,2.50,3.85,2.85,'2022年11月—2024年10月','超过 280 倍','同等基准水平模型调用价格下降\nStanford AI Index');text('口径：用户数非付费人数；内购覆盖广义生成式 AI；模型调用价格非实时语音全链路成本。',.66,5.65,12,.55,12,C.muted);end('引用三条相互独立的事实：国内AI已覆盖大量用户，全球消费者已经发生真实内购，达到同等基准水平的模型调用价格显著下降。这里引用历史观测数据，不用2026年的预测冒充实际，也不将模型价格下降等同于我们的毛利。');
//05
begin('MARKET','先进入明确场景，再逐步扩大服务市场','首发服务中国成年用户；从长期交流建立关系，向与本人生活直接相关的服务延伸。',['S1','S2']);
const scope=[['首发市场','希望被持续理解的中文成年用户','晚间回顾、生活变化、重要事情的接续'],['相邻市场','基于个人背景的生活协助','日程、目标、学习、出行与家庭事项'],['长期空间','围绕个人身份的服务连接','跨服务意图分发与跨设备连续体验']];scope.forEach((r,i)=>{shape(.64,2.48+i*1.13,12.03,.9,i===0?'D8E8D7':'E7EDE5');text(r[0],.87,2.70+i*1.13,1.5,.4,15,C.ink,true);text(r[1],2.52,2.65+i*1.13,5,.45,19,C.ink,true);text(r[2],7.77,2.70+i*1.13,4.6,.38,13,C.muted)});text('6.02 亿是采用基础，19 亿美元是广义 AI 内购规模；均不作为本项目 TAM 或收入预测。',.67,6.04,12,.35,11,C.muted);end('我们不把中国全部AI用户乘以一个虚构价格计算市场规模。首发市场由具体需求和渠道验证来界定。长期商业空间来自关系建立之后的服务扩展，而非在第一天覆盖所有生活场景。');
//06
begin('CATEGORY VALIDATION','Tolan 展示了陪伴走向生活入口的可能','投资方的公开描述：持续伙伴关系，正在延伸到购物、生日安排和家庭协作。',['S4']);
card(.6,2.50,3.85,2.65,'当月使用规模 · 投资方预计','25 万人以上','USV 2026年10月公告\n非付费用户数');card(4.74,2.50,3.85,2.65,'过去一年 · 投资方披露','7,000 万次以上','与 Tolan 的对话\n不是用户数');card(8.88,2.50,3.85,2.65,'2026年10月融资公告','3,000 万美元','USV 领投\nKhosla Ventures 参投');band('AvatarWeave 的机会：围绕中文生活语境，验证自己的关系、记忆与服务闭环。');end('Tolan证明这一产品方向获得了用户使用和资本支持。USV预计2026年10月有25万以上用户使用，披露过去一年7000万以上对话及3000万美元融资。上述为投资方口径，不能当作本项目的收入、留存或成功概率。我们借鉴演进路径，而非复制用户画像和规模。');
//07
begin('PRODUCT','同一个小织，接续同一个人的生活','以“重要面试”为首发体验，帮助投资人看见一次真实可理解的产品闭环。');
card(.60,2.53,3.85,2.85,'第一次 · 对话与许可','先听你说','用户表达紧张\n小织回应并确认关键信息\n由用户决定是否记住');card(4.74,2.53,3.85,2.85,'下一次 · 延续与修正','记得来龙去脉','在允许时接续面试话题\n时间变化可以纠正\n用户能看到记忆来源');card(8.88,2.53,3.85,2.85,'未来 · 授权服务','把准备变具体','经确认生成准备清单\n连接日历与提醒服务\n完成后更新状态');text('产品场景示意；前两步已有工程基础，第三步为扩展规划，不是现有客户案例。',.65,5.88,12,.4,12,C.muted);end('这里呈现一个完整的使用故事。当前价值是被理解并在下次继续交流，未来价值是基于同一段背景帮助完成准备。记忆许可、事实纠正和外部动作确认需要保持清晰边界。图示不是虚构的用户证言或上线截图。');
//08
begin('PRODUCT DESIGN','让“听见、记住、行动”在界面上看得见','产品界面设计示意 · 非上线截图');
for(let i=0;i<3;i++)shape(.88+i*4.16,2.48,3.25,3.78,'FFFFFF','CDD8CE',1);
text('与小织聊聊',1.13,2.77,2.8,.45,19,C.ink,true);image('assets/xiaozhi-ui.png',1.48,3.30,1.95,2.10);shape(1.15,5.50,2.7,.46,'DCEBDB');text('麦克风  ·  字幕  ·  暂停',1.28,5.63,2.5,.25,11,C.ink);
text('我们记得的事',5.29,2.77,2.8,.45,19,C.ink,true);text('最近在准备面试',5.29,3.5,2.8,.55,18,C.ink);text('查看来源\n修改时间\n不再记住',5.29,4.27,2.8,1.35,15,C.muted);
text('需要我帮你吗',9.45,2.77,2.8,.45,19,C.ink,true);text('将准备事项加入日历',9.45,3.50,2.8,.75,18,C.ink);text('执行前展示内容与范围\n你确认后才会写入',9.45,4.45,2.8,.75,13,C.muted);shape(9.45,5.48,2.7,.46,C.ink);text('确认并执行',9.55,5.57,2.5,.25,12,C.white,true,'center');end('投资人不需要先理解数据库，应该先看见产品价值。三个界面分别解决愿意表达、理解系统记了什么、控制外部行为。文字、卡片与按钮均可在PPT编辑。服务执行页是未来设计，不能在演示时称为现有功能。');
//09
begin('SYSTEM','用一套个人上下文，连接角色、模型与服务','角色是用户熟悉的交互对象；记忆和权限构成可持续的系统基础。');
const layers=[['交互层','语音 / 文字 / 卡通表达','现有基础'],['个人上下文层','人物 / 偏好 / 经历 / 目标 / 来源与纠正','现有基础，持续完善'],['授权执行层','意图确认 / 服务选择 / 执行回执 / 撤销与追踪','规划'],['服务与设备层','日历 / 学习 / 出行 / 内容 / 移动与其他终端','分阶段接入规划']];layers.forEach((a,i)=>{shape(.7,2.46+i*.88,11.94,.70,i===1?'D3E7D1':'E7ECE4');text(a[0],.95,2.63+i*.88,2.1,.34,16,C.ink,true);text(a[1],3.15,2.65+i*.88,6.8,.32,14,C.ink);text(a[2],10.3,2.65+i*.88,2.1,.32,10,C.muted)});end('长期架构要支持模型和角色迭代而不破坏用户上下文。现有工程覆盖语音与记忆基础，未来需要补齐可审计的授权执行层。系统将第三方工具输出视为待核验数据，执行要有回执，不能把模型说完成了当作服务真的完成。');
//10
begin('EXPANSION','入口从一次对话开始，在解决问题中扩大','发展路径按用户价值推进；每一步都需要新的产品证据。');
const st=[['伙伴','愿意主动回来','角色与自然交流'],['个人助理','建议符合本人背景','长期记忆与目标'],['服务入口','跨服务完成事情','授权连接与回执'],['持续身份','换设备仍然连续','账号与上下文迁移']];st.forEach((a,i)=>{card(.6+i*3.12,2.6,2.86,2.9,String(i+1).padStart(2,'0'),a[0],a[1]+'\n\n'+a[2]);});for(let i=0;i<3;i++)text('→',3.47+i*3.12,3.70,.28,.4,16,C.muted,true,'center');band('扩展顺序：先证明关系成立，再证明行动有用，最后扩大服务与终端。');end('未来入口不是一个空泛标签。第一阶段看用户是否主动回来，第二阶段看建议是否体现个人背景，第三阶段看服务是否可靠完成，第四阶段看换设备时关系能否延续。每个阶段都明确新增能力及验证问题，避免工程范围失控。');
//11
begin('COMPETITION','大平台争夺通用入口，我们从持续关系切入','语音、角色和服务连接已有竞争；差异要在中文生活场景与可信连续性上兑现。',['S4','S5','S6']);
const rows=[['通用 AI 平台','Gemini 等','服务连接与通用能力','用户规模与生态是强竞争'],['角色内容平台','Character.AI 等','角色选择与语音交流','语音本身不能构成壁垒'],['持续陪伴产品','Tolan','关系延续到生活协助','最直接的产品路径参照'],['AvatarWeave','中国市场 · 尚未商业化','卡通伙伴 + 可控记忆 + 授权行动','前两项已有基础；行动为规划']];
rows.forEach((a,i)=>{let y=2.5+i*.78;shape(.64,y,12.04,.66,i===3?'D3E7D1':'E7ECE4');text(a[0],.82,y+.16,2.05,.34,13,C.ink,true);text(a[1],2.9,y+.17,2.6,.32,11,C.muted);text(a[2],5.55,y+.16,3.85,.35,13,C.ink);text(a[3],9.46,y+.17,3.02,.35,10,C.muted)});text('比较依据为公开产品方向，不是完整功能审计；不声称其他产品缺少记忆、隐私控制或中文能力。',.69,5.92,12,.4,11,C.muted);end('我们面对的竞争包括系统级平台、角色平台和长期陪伴产品，不能靠假设对方没有某项功能建立优势。大平台分发优势明显，初创项目要先赢得一个具体人群的复用理由。中文只是起点，持续正确和可信的体验才可能形成防御性。');
//12
begin('DEFENSIBILITY','壁垒来自被验证的连续性，而非模型包装','每次更正、每次升级、每次服务结果，都要维护同一个人的可信上下文。');
card(.6,2.5,3.85,2.9,'可信记忆','记住变化','人物与时间有依据\n更正后停止使用旧信息\n用户能查看、删除和迁移');card(4.74,2.5,3.85,2.9,'产品学习','持续变好','用授权反馈积累评测案例\n评估跨日接续与失败场景\n模型升级保持角色一致');card(8.88,2.5,3.85,2.9,'品牌与信任','愿意长期选择','原创角色和稳定表达\n透明权限与服务边界\n实际完成事情的口碑');band('拟建立的竞争优势：准确性 × 连续性 × 信任 × 场景完成能力。');end('这些是正在建立的优势，不是已经形成的护城河。我们不以出售私人记忆为商业模式，也不假定用户越多模型就自动越好。产品学习来自获准的反馈和评测，留存依靠持续价值，而不是数据无法迁移。');
//13
begin('BUSINESS MODEL','先证明订阅价值，再拓展服务收入','当前尚未商业化；定价与收入结构需要真实付款验证。',['F1']);
card(.6,2.5,3.85,2.9,'第一阶段 · 主收入','个人订阅','用户为持续语音体验\n个人上下文与便利性付费\n用量分档控制边际成本');card(4.74,2.5,3.85,2.9,'第二阶段 · 价值扩展','服务增值','围绕已验证的生活需求\n提供可选高级服务\n合作和收费透明披露');card(8.88,2.5,3.85,2.9,'长期 · 独立商业选项','引擎与角色授权','自有产品验证后\n再评估合作品牌及终端\n不与首发产品争夺资源');text('记忆查看、纠正与删除不作为付费门槛；不以情感压力推动续费。',.68,5.88,12,.4,13,C.muted);end('融资初期先回答用户为什么续费。订阅提供与使用量匹配的服务，用户控制自己信息的权利不应被锁在付费墙后。未来服务佣金或授权收入必须基于实际合作另行建立，不放入当前收入预测。');
//14
begin('UNIT ECONOMICS','用真实账单决定定价，用队列决定扩张','当前尚未商业化，先建立可追踪的单用户服务成本与订阅队列。');
shape(.65,2.50,12.05,1.05,C.ink);text('单位贡献 = 净订阅收入 − 语音与模型 − 记忆维护 − 变动支持',.90,2.82,11.55,.45,21,C.white,true,'center');
card(.65,3.9,3.8,2.4,'测成本','一次完整服务','实际语音分钟、模型调用\n后台任务、存储和失败重试');card(4.77,3.9,3.8,2.4,'测转化','一批真实用户','激活、留存、付款、退款\n区分自然流量与广告用户');card(8.89,3.9,3.8,2.4,'测回收','持续现金贡献','加入试用、渠道与客服成本\n验证后才增加获客预算');end('尚未商业化意味着不能给出可信的历史毛利和LTV。第一步是按真实请求计量成本，第二步用同批成熟用户的付款和留存计算贡献，第三步将免费试用和渠道开支计入回收。额度要参考高使用量用户的成本尾部，而非只看平均值。');
//15
begin('GO TO MARKET','用“第二次见面”让用户理解产品价值','首发人群：愿意与卡通伙伴交流、希望生活经历能被持续接续的中国成年人。');
card(.60,2.5,3.85,2.9,'触达','演示关系连续性','展示一次对话与跨日接续\n用自有账号发布产品过程\n小规模测试内容与渠道');card(4.74,2.5,3.85,2.9,'激活','尽快交付有用体验','一个固定角色自然开场\n明确记忆许可和控制\n让用户有理由主动回来');card(8.88,2.5,3.85,2.9,'复用与付费','围绕用户的真实事情','观察生活事件是否接得上\n访谈留下与离开的原因\n付款后再测试渠道可复制性');band('渠道优先验证：小红书 / 短视频 / 兴趣社群；这些是拟测试渠道，尚无签约或转化成果。');end('获客不只展示角色颜值，而要让用户看到产品能接续他自己的经历。先用创始人产品记录、合成场景演示和邀请测试找到明确需求，再对有效渠道投入。真实用户内容公开前需获得许可。');
//16
begin('CURRENT PROGRESS','已有可运行基础，下一步是商业验证','当前状态：全职独立开发，尚未商业化。工程积累支持起步，不能替代用户证据。',['P1','F1']);
card(.6,2.5,3.85,2.9,'已实现与开发验证','语音和角色','LiveKit 实时交流\nLive2D 渲染、口型与动作\n多供应商管线配置');card(4.74,2.5,3.85,2.9,'已实现与开发验证','记忆与控制','跨会话检索与接续\n来源、纠正、删除、导出\n账号与会话恢复');card(8.88,2.5,3.85,2.9,'本轮要完成','产品与经营闭环','正式稳定性和隐私验收\n角色成品、支付与权益\n真实使用、付费和成本验证');text('本页据仓库开发记录与当前代码；无生产规模、留存率、付费用户或收入声明。',.66,5.89,12,.4,12,C.muted);end('已有代码让项目能够以较低的重新起步成本进入产品测试。开发记录已经包含真实语音和记忆连续性的验证，但尚未完成生产联合验收。本次路演不将测试次数或合成账号当作商业用户，也不把未来能力包装成已经上线。');
//17
begin('MILESTONES','用三次验证，把工程原型变成可投资的业务','建议资金窗口内按成果释放预算；未来入口的每一步都建立在上一阶段证据上。');
card(.6,2.5,3.85,2.9,'第 1—3 月 · 计划','可靠的陪伴产品','语音、记忆与账号稳定\n用户能理解并控制信息\n核心缺陷可定位、可恢复');card(4.74,2.5,3.85,2.9,'第 4—9 月 · 计划','真实的订阅理由','成熟队列持续复访\n形成真实付款和续费证据\n账单支持可持续贡献');card(8.88,2.5,3.85,2.9,'第 10—15 月 · 计划','入口的扩展证据','选一个生活服务场景\n完成授权、执行与回执\n证明跨场景使用有增量');band('扩张条件：质量、留存与贡献共同成立；未达成前不扩角色、硬件或大规模投放。');end('这轮资金不是购买未经验证的用户增长，而是购买关键不确定性的答案。时间排期需要在招聘和供应商报价确认后落实。这里不填没有基线的增长承诺，先冻结评估口径，再确定可执行的目标及后续融资节点。');
//18
begin('RISK & CONTROL','个人入口必须先赢得长期信任','把风险控制放进产品设计和执行流程，保护未来的扩展空间。');
const risks=[['记忆错误与隐私','来源、纠正、删除贯通验证；备份恢复不让已删内容复活'],['语音成本与可靠性','用量分档与限额；多供应商回归；失败退款与服务补偿'],['关系边界与内容','面向成年人；清晰AI身份；不以情感施压诱导付费或使用'],['服务执行与平台竞争','逐项授权、确认与回执；从可逆动作开始；聚焦具体场景']];risks.forEach((a,i)=>{let y=2.5+i*.82;shape(.65,y,12.05,.69,i%2?'E5ECE3':'ECF0E9');text(a[0],.87,y+.16,3.05,.36,16,C.ink,true);text(a[1],4.02,y+.18,8.35,.35,13,C.muted)});end('风险既影响用户，也影响商业持续性。记忆错误会损害信任，长时间语音可能拖累贡献，角色表达需要保持清晰边界，外部行动必须可确认和追踪。中国上线前需要围绕实际产品与数据流完成适用要求审查，此处不宣称已完成合规或取得特定资质。');
//19
begin('FOUNDER','路伟：把算法经验转化为完整个人 AI 产品','创始人提供的履历 · 硕士 · 全职独立开发 · 8 年算法研发经验',['F1']);
shape(.65,2.52,3.6,3.42,C.ink);text('路伟',.95,2.93,2.9,.9,39,C.white,true);text('Founder\n全职 · 独立开发',.98,4.05,2.9,1.0,20,C.mint);text('好未来 / 爱学习',4.72,2.73,7.7,.55,25,C.ink,true);text('长期从事人工智能行业',4.74,3.50,7.65,.45,18,C.muted);text('时间序列预测\n量化交易\n情感陪伴数字人',4.74,4.25,7.65,1.45,21,C.ink);end('创始人路伟为硕士，全职独立开发，有8年算法研发经验，经历包括好未来和爱学习，代表方向包括时间序列预测、量化交易和情感陪伴数字人。本页依据创始人提供的信息，未添加职位、学校、业绩数字或雇主背书。融资后需要补齐产品设计、增长和生产运维能力。');
//20
begin('FINANCING REFERENCE','同类项目把资金投入工程、角色和使用扩展','公开信息支持资金用途方向，未披露精确分配比例；不据此套用海外融资额。',['S7','S8','S4']);
card(.6,2.5,3.85,2.95,'Tolan · 2025年2月','1,000 万美元','创始人披露融资\n同时招聘工程、动画、\n设计与写作人才');card(4.74,2.5,3.85,2.95,'Tolan · 2025年7月','2,000 万美元','媒体报道 A 轮\n继续招聘工程人才\n为免费版推理开支做准备');card(8.88,2.5,3.85,2.95,'Tolan · 2026年10月','3,000 万美元','USV 投资公告\n描述关系向生活协助延伸\n并未披露预算明细');text('对 AvatarWeave 的启示：先投入体验和可靠性，再用受控预算验证使用与付费。',.68,5.9,12,.35,13,C.ink,true);end('海外案例用于说明钱花在什么能力上，而不是拿来锚定本项目估值。2025年2月的创始人文章明确招募工程与创作人才；7月报道说明扩张免费使用前的推理成本考虑；2026年公告展示生活服务扩展。上述没有披露各项费用比例。');
//21
begin('FUNDING PROPOSAL','建议融资 300 万元，完成 15 个月的关键验证','人民币 · 天使 / 种子阶段融资建议 · 预算上限，非历史成本或已获融资');
text('300 万',.70,2.62,4.0,1.0,49,C.ink,true);text('15 个月建议窗口\n全口径平均上限 20 万元 / 月',.75,3.89,4.1,.95,18,C.muted);text('资金要换来：\n可靠产品 → 真实付费 → 服务扩展证据',.75,5.12,4.1,.95,16,C.ink,true);
const bud=[['核心团队',150],['角色与内容',30],['模型、云与评测',30],['用户研究与获客试验',35],['隐私、法务与安全支持',10],['工具与运营',15],['风险准备金',30]];bud.forEach((a,i)=>{let y=2.48+i*.48; text(a[0],5.5,y,4.65,.30,14,C.ink);text(a[1]+' 万',10.8,y,1.7,.30,14,C.ink,true,'right');shape(5.5,y+.34,7.0,.035,'D8E2D7');shape(5.5,y+.34,7*a[1]/150,.035,C.mint)});text('金额与人力参数需招聘及供应商报价复核；分阶段招聘，不承诺估值、出让比例或收入。',5.5,6.0,7.0,.35,10,C.muted);end('建议融资300万元人民币，计划窗口15个月，优先实现可靠产品、真实订阅证据和一个授权服务闭环。150万元人力预算按平均4个全职等效岗位、每岗位每月2.5万元全口径成本、15个月测算，包含创始人岗位，这是预算约束而非当前人数或市场薪酬报价。其余预算150万元含30万元准备金，总计300万元。不依靠未来收入抵减资金需求，不给估值或稀释建议。');
//22
begin('VISION','','',[],true);image('assets/future-entry-vision.png',0,0,13.333,7.5);text('AVATARWEAVE',.68,.55,7,.45,17,C.mint,true);text('当 AI 记得你的生活，\n入口就有了连续性。',.68,1.9,6.4,1.6,34,C.white,true);text('从卡通伙伴出发\n成为理解你、连接服务的个人 AI 入口',.72,4.2,5.7,.95,19,C.mint);text('路伟  /  创始人\n中国市场 · 全职开发 · 尚未商业化',.72,5.7,5.7,.65,13,C.white);end('我们希望寻找认同长期个人AI方向、能帮助产品和中国用户增长的早期投资伙伴。当前不是以虚构收入换估值，而是以已有执行基础、明确产品切入和受控资金计划，验证未来个人入口的可能性。联系信息在实际投递前由创始人填写。');
//23
begin('APPENDIX · BUDGET','融资预算与执行边界','测算性质：拟议预算约束；待实际招聘与供应商报价后形成执行预算。');
text('核心团队 150 万元',.70,2.48,11.8,.55,24,C.ink,true);text('平均 4 个全职等效岗位 × 2.5 万元 / 岗位月 × 15 个月\n包含创始人岗位；全口径含现金薪酬、雇主承担成本等；不是当前团队人数。',.74,3.20,11.8,.80,16,C.muted);
text('运营与验证 120 万元  +  准备金 30 万元',.70,4.35,11.8,.55,24,C.ink,true);text('角色 30 + 模型云评测 30 + 研究获客 35 + 隐私法务安全 10 + 工具运营 15 = 120 万元\n270 万元计划开支对应平均 18 万元 / 月；另保留 30 万元应对偏差。',.74,5.10,11.8,.85,15,C.muted);end('融资建议的范围是预算设计，并非真实经营数据。人员配置方向可由创始人统筹产品与算法，加工程与记忆、客户端与设计、用户运营等能力，通过全职、阶段性招聘和外部制作组合实现。现有设备、创始人现金投入和供应商报价未提供，因此不编造资产、历史投入或价格。若实际报价无法落入预算，应调整招聘节奏和验证范围。');
//24
begin('APPENDIX · SOURCE REGISTER','市场与行业数据的来源和口径','访问日期：2026 年 11 月 5 日。数据页均保留编号，完整链接也写入演讲者备注。');
['S1','S2','S3','S4'].forEach((id,i)=>{const a=sources[id],y=2.4+i*.97;text(id+'  '+a.name,.7,y,11.9,.33,15,C.ink,true);text(a.date+'｜'+a.claim,.72,y+.37,11.85,.49,10.5,C.muted)});end('本附录用于核验引用。使用规模、应用内购、模型价格和融资金额采用不同口径，不能相互替代或直接推导本项目市场份额。公开数据只能说明外部环境，项目经营证据仍需建立。');cur.refs=['S1','S2','S3','S4'];s.addNotes(['S1','S2','S3','S4'].map(id=>id+' '+sources[id].url).join('\n'));
//25
begin('APPENDIX · SOURCE REGISTER','竞品、融资与公司事实的依据','对外资料与项目自述分开使用；不添加无法核验的排名、独家能力或投资背书。');
['S5','S6','S7','S8','P1','F1'].forEach((id,i)=>{const a=sources[id],y=2.36+i*.60;text(id+'  '+a.name,.70,y,4.8,.31,13,C.ink,true);text(a.claim,5.45,y,7.1,.50,10.3,C.muted)});end('公开产品文档用于确认竞争方向，不构成完整功能比较。融资报道只能说明当时披露的信息；团队与尚未商业化状态由创始人确认。投递前建议准备教育及工作经历、IP归属、主体和股权、真实成本账单及开发验证材料，供投资尽调。');cur.refs=['S5','S6','S7','S8','P1','F1'];s.addNotes(cur.refs.map(id=>id+' '+sources[id].url).join('\n'));

(async()=>{
 await pptx.writeFile({fileName:path.join(root,'AvatarWeave-Investor-Deck-V2.pptx')});
 const font='/tmp/avatarweave-NotoSansCJKsc-Regular.otf';const b64=fs.readFileSync(font).toString('base64');
 const css=`@font-face{font-family:BP;src:url(data:font/otf;base64,${b64})}*{box-sizing:border-box}html,body{margin:0;padding:0;background:#cdd8d1;font-family:BP,sans-serif}.slide{position:relative;width:1280px;height:720px;margin:24px auto;overflow:hidden;background:#${C.bg}}.txt{overflow:visible}img{display:block}@page{size:13.333333in 7.5in;margin:0}@media print{html,body{background:white}.slide{margin:0;break-after:page}.slide:last-child{break-after:auto}}`;
 fs.writeFileSync(path.join(root,'deck.html'),'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>AvatarWeave Investor Deck V2</title><style>'+css+'</style><body>'+all.map(a=>`<section class="slide" data-slide="${a.id}" style="background:#${a.dark?C.dark:C.bg}">${a.html.join('')}</section>`).join('')+'</body></html>');
 fs.writeFileSync(path.join(root,'deck-content.json'),JSON.stringify(all.map(({html,...a})=>a),null,2));
 fs.writeFileSync(path.join(root,'sources.json'),JSON.stringify(sources,null,2));
 fs.writeFileSync(path.join(root,'speaker-notes.md'),'# AvatarWeave V2 路演讲稿\n\n'+all.map(a=>'## '+a.id+' '+(a.title||'愿景')+'\n\n'+a.notes+'\n\n'+a.refs.map(id=>`[${id} ${sources[id].name}](${sources[id].url||'#'})`).join(' · ')).join('\n\n'));
 fs.writeFileSync(path.join(root,'funding-budget.csv'),'\ufeff项目,建议预算万元,性质\n'+bud.map(a=>`${a[0]},${a[1]},融资预算建议非历史实际`).join('\n')+'\n合计,300,待报价复核\n');
 console.log('Created '+all.length+' editable slides, notes, source ledger, and budget.');
})();
