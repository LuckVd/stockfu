# Research: 2025–2026 年中国 A 股风格因子/异象实证调研（为扩展量化选股策略服务）

> 调研目标：为现有策略（价值、股息、动量、反转、低波、低β、规模、流动性）补充**质量/成长/盈利**与**行业轮动**维度，重点考察：①近年实证有效性；②与价值因子的正交/互补性；③用日线数据是否可复现。
> 调研日期：2026-08-13（researcher 子代理，检索时间窗口以 2024-2026 年文献与券商研报为主）。

---

## Summary（直接回答）

1. **质量/盈利因子在 A 股长期有效、且与价值因子天然互补（负相关），2024–2026 重新走强，是当前最有把握的扩展方向**：2010–2024 年区间内价值、红利、低波、质量、现金流五类因子有效而成长、动量偏弱（[中证指数数据](http://caifuhao.eastmoney.com/news/20250323214830650058050)）；学术实证还发现 A 股"价值与综合盈利负相关且逐年强化"，价值溢价走弱部分正源于负盈利暴露，质量/盈利是价值的对冲面（[IRFA 2024](https://ideas.repec.org/a/eee/finana/v94y2024ics1057521924002576.html)）。2025–2026 年券商风格月报一致显示"高盈利、高估值"因子主导（[中银国际风格月报 2026-06](http://www.cy-mmm.com/doc-6eb832f79e228e0785004a26b078f23c.html)、[衡泰 2026 因子报告](https://www.xquant.com/xinwendongtai/2026/1729.html)）。注意：**简单历史 ROE 因子弱，"稳定性/预期"改进版才显著**（[招商证券 PB-ROE 系列](https://wap.hibor.com.cn/data/f13c4a1e2c1a840a1df94cd4460c3fe7.html)、[华证新质量因子](https://finance.sina.com.cn/esg/2025-03-13/doc-inepnynm0662312.shtml)）。
2. **成长因子 2025–2026 在成长行情下弹性大，但高增速组内部分化极大，单独预测"高增速"不足以稳定超额，必须叠加二次选股（行业轮动因子等）**；线性三因子（已披露增速+成长+分析师）优于树模型/GRU 等非线性模型（[申万金工因子观察第10期 2026-05](https://finance.sina.com.cn/wm/2026-05-28/doc-inhzmayw0285639.shtml)）。
3. **新异象方面，日线可复现的增量 alpha 集中在：隔夜/日内收益分解（A 股独有的负隔夜溢价与"拔河效应"）、MAX/偏度类彩票异象、量价微观结构（成交笔数切割反转、潮汐）、低关注度/热点反转**；但大规模复制检验（[Management Science 2024](https://ideas.repec.org/a/inm/ormnsc/v70y2024i8p5066-5090.html)）显示美股 469 个异象中约 **83–87% 在 A 股（VW 收益、剔除微盘口径下）不显著**，必须用 A 股口径自行验证。行业轮动上，**行业动量 2021 年前强、2022 年后衰减，需"动量择时+拥挤度惩罚"改造**；ETF 资金流、大小单资金流 2024–2025 实证年化超额 6.5%–20%（[华泰](https://www.nxny.com/report/view_5777065.html)、[招商](https://www.nxny.com/report/view_5870475.html)、[方正](https://www.nxny.com/report/view_5848303.html)）。

---

## Findings

### 一、质量 / 成长 / 盈利因子（2024–2026 实证）

1. **质量、盈利因子长期有效且近两年走强** — MSCI 研究报告（2025-12）指出 A 股高股息与低波因子一贯领先、与全球"动量领先"的惯例不同，纯因子中 value/growth/quality 长期跑赢（[MSCI](https://www.msci.com/research-and-insights/paper/are-you-really-capturing-the-right-factors-unlocking-deeper-insights-in-china-a-share-factor-investing)）；中证指数官方数据（2010 底–2024 底）显示价值、红利、低波、质量、现金流五类因子有效，成长、动量较弱，且**等权加权超额高于自由流通市值加权**（[东方财富/中证](http://caifuhao.eastmoney.com/news/20250323214830650058050)）。2026 年 6 月衡泰因子报告：2007–2026 年逐年因子收益率中"盈利因子几乎每年为正"，2026 年 6 月风格切换、市值因子大幅反转（[衡泰](https://www.xquant.com/xinwendongtai/2026/1729.html)）；中银国际 2026-06 风格月报：二季度"高盈利、高估值"主导，财报窗口期高 ROE、净利润增速超预期公司受青睐（[中银国际](http://www.cy-mmm.com/doc-6eb832f79e228e0785004a26b078f23c.html)）。Premia 2025Q4 因子回顾也显示四季度由成长切向价值/质量/低波防御（[Premia Q4 2025](https://www.premia-partners.com/insight/china-a-shares-q4-2025-factor-review)）。

2. **简单 ROE 因子弱，"稳定性"是关键改进** — 招商证券 PB-ROE 系列：历史 ROE 水平高的个股未来表现不及预期，基于**未来 ROE** 的多头组合才显著超额，原因在于 ROE 多头稳定性不足；引入 ROE 稳定性因子后多头组合显著改善（[招商证券](https://wap.hibor.com.cn/data/f13c4a1e2c1a840a1df94cd4460c3fe7.html)）。华证新质量因子（2025-03，样本 2010–2024）：针对"单维 ROE 失效年份无法控回撤"和"混入成长指标不纯粹"两大缺陷，用定价能力（销售净利率）+资本效率（GPOA）+市场地位（营收份额）+财务真实性交叉验证、全部用长期稳定性处理，RankIC 均值由 2.2% 提升至 3.4%，大/中盘 RankIC 达 5.48%/4.58%（IR 1.44/1.40），**与其他风格因子相关系数绝对值均 <0.6（与原质量 0.81、与规模 0.52）**，熊市防御突出（2015/2018/2023 熊市跌幅均优于全指）——既回答了"是否有效"，也回答了"与价值正交性"（[华证](https://finance.sina.com.cn/esg/2025-03-13/doc-inepnynm0662312.shtml)）。

3. **质量溢价有独立于三/五因子的学术证据** — 天津大学 2025 年研究用 90+ 财务指标 + PLS 等机器学习构建基本面质量综合指数，对横截面收益预测力最强（多头年化近 38%），CAPM/三因子/五因子均不能解释（[管理科学学报](https://jmsc.tju.edu.cn/jmsc/article/abstract/20250211?st=search)）；基于预期盈余增长（EEG）的质量因子加入 FF3 后四因子模型定价效率更高（[东北大学学报 2024](https://xuebao.neu.edu.cn/natural/CN/Y2024/V45/I1/145)）；"品质溢价"（盈利+成长+安全+分红四维）在 A 股显著为正、且错误定价解释而非风险补偿（[金融研究](http://www.jryj.org.cn/CN/abstract/abstract670.shtml)）。

4. **与价值因子的关系：负相关、互补而非正交独立** — 学术实证发现 A 股价值与综合盈利（盈利水平+盈利增长）呈**负相关且逐年强化**，叠加稳定的盈利溢价，正是近年价值溢价走弱的重要原因（[IRFA 2024 "Anatomy of recent value premium's travails"](https://ideas.repec.org/a/eee/finana/v94y2024ics1057521924002576.html)）。业界层面：财新智能贝塔专题明确"价值与质量考量因素不同、收益特征不同，互为补充"（[财新](http://index.caixin.com/upload/smartbeta03/caixinsmartbeta0302.pdf)）；东方证券 PB-ROE 策略研究指出三重收益来源（低 ERP 的估值防御 + 高盈利复利 + 预期偏差），且更适应金融、消费、周期板块（[东方证券](http://msd.microbell.com/data/03d54c759066806eb9789833f5372cc0.html)）；华创金工实证发现估值因子收益来自"估值与基本面错配"的价值发现而非风险补偿，盈余公告日超额（12 个交易日内累计 2.3%）提示财报窗口是基本面策略收益兑现期（[华创证券](https://www.hibor.com.cn/repinfodetail_4323939.html)）。**结论：给现有价值策略叠加质量/盈利因子，历史上是对冲价值回撤（2021–2023）而非放大风险的有效手段。**

5. **成长因子 2025–2026 有效但"分化"是最大问题** — 申万金工因子观察第10期（2026-05）：若提前知道未来净利润增速并持有前 10% 高增速股，组合平均收益出色（含 2022–2023 熊市），但**中位数收益弱、约 40% 个股贡献主要涨幅、60 分位起收益才显著抬升**；用"已披露单季净利增速+成长因子+分析师因子"三因子等权 Top50，命中下期前 10% 增速的占比 49.17%，但**误选的高增速候选股系统性弱于同档真实增速组（"未命中代价"）**；**叠加行业轮动因子做二次选股可显著增厚收益**；XGBoost/GRU 等非线性模型未超越线性基准（[申万金工](https://finance.sina.com.cn/wm/2026-05-28/doc-inhzmayw0285639.shtml)）。申万宏源《成长因子2.0》（2025-06）：先预测下一年净利润增长、再做成长因子筛选的组合显著跑赢（TMT 板块已验证，逻辑同"红利增长"研究）（[申万宏源](https://www.nxny.com/report/view_5987549.html)）。源达信息十年回测：2022 年以来营收/净利增速 0–15% 的中速增长组最优，与大盘转价值风格一致（[源达](https://www.fxbaogao.com/detail/4997537)）。

6. **成长因子的构造口径决定成败** — 华证新成长因子：短期（扣非净利 TTM 环比、扣非 ROE_TTM 环比、单季扣非净利/ROE 同比）+ 中长期（标准化预期外盈利/收入），并强调"传统高 PB=成长"的 Fama-French 口径在 A 股很粗糙（[华证](https://finance.sina.com.cn/esg/2025-03-07/doc-inenvfqi5841292.shtml)）；华泰单因子测试：季度增速类 Sales_G_q / Profit_G_q / ROE_G_q 分层、回归、IC 三项测试均最优（[华泰/BigQuant](https://bigquant.com/wiki/doc/r189gp7JK9)）。

### 二、与价值正交的新异象（2024–2026 实证）

7. **冷水先行：A 股异象复制率很低** — Management Science 2024（Li/Liu/Liu/Wei）：按 Hou-Xue-Zhang 口径复制 469 个异象，**83.37% 无显著 H-L 原始收益差；CAPM alpha 下 84.22%、FF3 alpha 下 86.99% 不显著**；全 A 断点+等权会过度给微盘权重、容量有限，须用主板断点+VW 收益；q 因子模型和 CH3/CH4 在 A 股表现最好（[Management Science](https://ideas.repec.org/a/inm/ormnsc/v70y2024i8p5066-5090.html)）。SSRN《Finding Anomalies in China》更严：454 个策略经多重检验校正（t≥2.85）后仅 38 个原始收益显著、风险调整后为 0（[SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4322815)）。**含义：新增因子必须用 A 股自己的口径（剔除微盘、VW、多重检验）验证，不能直接照搬美股结论。**
   - 与之呼应的正面证据：Pacific-Basin Finance Journal 2021 检验 32 个异象，value、risk、trading 类在 A 股成立，size/quality/past-return 类弱，但存在很强的**残差动量与反转**（[PBFJ 2021](https://ideas.repec.org/a/eee/pacfin/v68y2021ics0927538x21001141.html)）——这与现有策略的动量/反转形成交叉验证。

8. **隔夜收益异象：A 股独有、日线可复现、与价值正交** — A 股平均隔夜收益（C2O）统计上显著为负（"隔夜负收益之谜"），机制是 T+1 制度导致的开盘价折价（[Economic Modelling 2020](https://ideas.repec.org/a/eee/ecmode/v89y2020icp55-71.html)、[金融研究 T+1 视角](http://dianda.cqvip.com/Qikan/Article/Detail?from=Qikan_Article_Detail&id=67837482504849574856484850)）；隔夜与日内收益存在"拔河效应"（[系统工程理论与实践 2020](https://sysengi.cjoe.ac.cn/EN/10.12011/SETP2020-1958)）。2025 年新证据：隔夜收益对个股开盘后半小时收益负向预测、负隔夜收益时更强（[Applied Economics 2025](https://ideas.repec.org/a/taf/applec/v57y2025i43p6933-6947.html)）；中信建投 2025-11 构建 32 个隔夜-日内细分因子（动量/波动/量价背离）+ d-LE-SC 有向图聚类领先-滞后策略，中证 500 滞后组年化 14.99%、alpha 11.34%（[中信建投](https://finance.sina.com.cn/wm/2025-11-26/doc-infystcp1980904.shtml)）；西部证券 2024-11"隔夜-日内拉锯因子"（[西部证券](https://www.hibor.com.cn/repinfodetail_3714361.html)）。**可复现性：只需日线开盘/收盘价，与价值因子相关性极低，是最值得优先自测的新维度。**

9. **MAX/偏度类彩票异象显著且稳健** — A 股存在显著 MAX 异象（月内最大日收益负向预测下月收益），1995–2017 多空组合年化 15.72%，套利限制强化该异象（[金融研究 2020](http://www.jryj.org.cn/CN/Y2020/V476/I2/167)）；**受涨跌停干扰 MAX 低估彩票需求，修正版 RMAX 更优**（[IRFA 2021](https://www.sciencedirect.com/science/article/abs/pii/S1059056021000149)）；中国市场层面已实现偏度负向预测未来收益（样本外 R²≈2.24%）（[已实现偏度研究](https://core.ac.uk/download/pdf/323959067.pdf)）；且 MAX 效应在 A 股不随规模减弱（[JEF 2016](https://www.sciencedirect.com/science/article/abs/pii/S0378426616302588)）。**可复现性：纯日线。注意与现有低波因子可能共享彩票偏好维度，需正交化检验。**

10. **量价微观结构：成交笔数切割反转（"理想反转"）** — 东吴证券"订单簿的温度"系列：用成交笔数对传统反转因子切割得到理想反转因子，IC 均值 -0.057、rankIC -0.070，多空年化 19.3%、月度胜率 74.3%、IR 2.51，剔除 Barra 风格与行业因子后 IR 升至 2.97（[东吴证券](https://bigquant.com/wiki/doc/4Um8LjwmYA)）。**数据要求：成交笔数（逐笔/部分日线库含成交笔数字段）；若只有量价日线，可用换手率/成交额波动近似但效果打折。** 量价趋势类：价格/成交量趋势越强未来收益越低，信息不对称高的股票上更强（[上海交大 2023](https://xtglxb.sjtu.edu.cn/CN/10.3969/j.issn.1005-2542.2023.04.011)）；方正"潮汐"因子（日内量能由低到高再回落的形态）亦有增量（[方正金工](https://bigquant.com/wiki/doc/RAsVk6yDUx)）。

11. **低关注度/热点反转：证据丰富但部分因子 2024 年后阶段性失效** — 关注度效应：股吧发帖关注度当月推高收益、下月反转，小市值多空组合年化超额 28.4%（支持 Merton"投资者认知"假说）（[关注效应研究](http://dbase.gslib.com.cn:8000/DRCNet.Mirror.Documents.Web/docview.aspx?DocID=4455184&leafID=15099)）；散户代码搜索关注度在行业与个股维度存在长期反转（[上交大学报](https://xtglxb.sjtu.edu.cn/CN/abstract/abstract2053.shtml)）；"增量关注度因子"rankIC -0.043、多空年化 9.04%（[21财经/兴业策略 2022](https://m.21jingji.com/article/20220706/herald/2d2a005494a691b07de1e51b3815ed0c.html)）；方正金工"热点漂移"（周度 IC 3.7%、年化 ICIR 5.53、多空年化 27.3%）与"热点反转"（周频 IC 4.54%、多空年化 19.1%）因子 2025 年发布、表现强，但同系列"凸显效应"因子在 **2024 年 9 月市场反弹后阶段性失效**（高热股票动量更持久所致）（[方正金工 2025](https://finance.sina.com.cn/roll/2025-03-13/doc-inepnyni3523740.shtml)）。**可复现性：用换手率/异常成交量做代理纯日线可得；发帖/搜索数据需外部数据源。**

12. **拥挤度：公开实证偏谨慎** — 招商证券（2020）构建估值价差、配对相关性、因子波动率、因子长期反转等 8 个拥挤度指标，XGBoost/LSTM 择时未明显优于纯做多，仅合成指标加权小幅战胜等权（[招商证券](https://bigquant.com/wiki/doc/XdhGA7uRsO)）。更务实的用法是把拥挤度作为**行业动量/抱团因子的惩罚项**（见下条招商 2025 行业动量改进）。

13. **涨停效应：主要作为情绪择时层而非选股因子** — 国泰海通 2025：打板策略收益、涨停/跌停占比等因子构建情绪择时模型，组合年化 6.65%（基准 3.82%）、回撤远小于全 A（[国泰海通](https://news.qq.com/rain/a/20250515A09EJF00)）；涨跌停制度会强化投资者处置效应并影响定价（[金融研究 2024](http://www.jryj.org.cn/CN/Y2024/V531/I9/153)）；涨跌停板存在跨市场溢出（[系统工程理论与实践 2025](https://sysengi.cjoe.ac.cn/CN/10.12011/SETP2024-0766)）。**注意：涨停数据日线可得，但打板类策略换手极高、容量小，与低频价值策略组合时收益贡献主要在情绪择时而非选股。**

### 三、行业轮动 / 行业中性化实证（2024–2026）

14. **行业动量：2021 年前强、2022 年后衰减，需要"动量择时+拥挤度惩罚"改造** — 招商证券（2025-02）：宏观周期+动量结合时动量几乎解释全部收益；行业指数本质是龙头股样本故动量强（龙头信息透明度高、被大资金追随）；失效环境为①熊市②高波动/牛熊快速切换③行业拥挤度过高；改造四步：拆分龙头动量+非龙头反转、按 PE/PB 中位数与波动率划分牛熊震荡市、分环境启用动量或反转、加拥挤度惩罚 → **改进后行业轮动 Rank IC 达 9.67%，TOP5 行业 2010 年以来年化 16.20%、超额约 11.08%**，ETF 组合落地（2018 年以来年化 18.46%/25.23%，IR 最高 1.14）（[招商证券](https://www.nxny.com/report/view_5870475.html)）。开源证券"行业轮动3.0"同样指出 2022 年后行业动量延续性减弱、轮动速度明显加快（[开源证券](https://www.fxbaogao.com/detail/4622942)）。

15. **资金流因子有真实增量预测力** — 华泰证券（2024-10）：ETF 大额资金流造成短期供需失衡，基于 ETF 资金流的**周频行业轮动**样本内（2018.1–2024.7）年化 >20%、Sharpe>1、月开仓胜率近 70%、盈亏比 >1.4（[华泰证券](https://www.nxny.com/report/view_5777065.html)）；开源证券（大小单资金流系列 23）：**主动超大单净流入（市值标准化）**为最优资金流因子，加入"高位/极端流"改进后 RankICIR 0.95→1.16、多空收益波动比 0.74→1.03（[开源证券](https://www.fhyanbao.com/rpview/1285081)）；渤海证券复合行业轮动（量价+基本面+资金面）：RSI 因子年化超额 6.6%（IR 0.683）、BIAS 5.5%（IR 0.585），复合模型回撤控制优于单因子（[渤海证券](http://m.hibor.net/wap_detail.aspx?id=eaf398e545438631a23e6aa6d56adb70)）；华安证券（2024-12）：被动化浪潮下 ETF 净流入因子预测力**由负转正**、权重股动量增强但"被动抱团"带来羊群风险（[华安证券](https://www.hibor.com.cn/repinfodetail_3814184.html)）；国金证券（2025-12）按 ETF 持有人结构（保险/国资等中长期资金）拆解资金流构建行业轮动（[国金证券](https://stock.finance.sina.com.cn/stock/go.php/vReport_Show/kind/9/rptid/819291735271/index.phtml)）。

16. **行业轮动的现实业绩与中性化建议** — 方正证券 2024 年行业轮动组合超额 **6.57%**，且指出 2024 年申万一级行业分化巨大（银行 +34.39% vs 医药 -14.33%），行业配置是超额收益重要来源（[方正证券](https://www.nxny.com/report/view_5848303.html)）；行业轮动在 A 股日频与月频区间存在动量、周频呈周期性轮动（[Atlantis 研究](https://www.atlantis-press.com/article/125972430.pdf)）；关于中性化：理论+实证结论为**多空投资者应做行业中性化、纯多头不应做**（因子在行业层面的收益贡献显著，对冲掉会损失收益）（[行业中性化研究](https://bigquant.com/wiki/doc/WcFDXPplAT)）；申万金工证实成长类因子在行业间差异大、与市值相关性总体弱（[华泰单因子测试](https://bigquant.com/wiki/doc/r189gp7JK9)）。**对现有策略的含义：行业轮动可作为持仓层面的行业偏离层（叠加行业中性选股），2024–2025 年实证年化超额 6.5%–20%，与个股因子正交性好。**

---

## Sources

### 保留（关键实证来源）
- **Replicating and Digesting Anomalies in the Chinese A-Share Market**（Management Science 2024）(https://ideas.repec.org/a/inm/ormnsc/v70y2024i8p5066-5090.html) — 469 异象复制，83–87% 不显著；A 股异象检验的方法论基准，直接指导可复现性设计。
- **Anatomy of recent value premium's travails**（IRFA 2024）(https://ideas.repec.org/a/eee/finana/v94y2024ics1057521924002576.html) — 价值与盈利负相关解释价值溢价走弱，是"质量补价值"的核心学术依据。
- **华证新质量因子**（2025-03）(https://finance.sina.com.cn/esg/2025-03-13/doc-inepnynm0662312.shtml) — RankIC 2.2%→3.4%、与风格因子 |ρ|<0.6、大中盘适用，业界质量因子最佳公开样本。
- **华证新成长因子**（2025-03）(https://finance.sina.com.cn/esg/2025-03-07/doc-inenvfqi5841292.shtml) — 成长因子构造口径（短期环比+预期外），避免"高 PB=成长"误区。
- **申万金工因子观察第10期**（2026-05）(https://finance.sina.com.cn/wm/2026-05-28/doc-inhzmayw0285639.shtml) — 成长预测最新实证：分化问题、未命中代价、行业轮动二次选股、非线性模型无效。
- **中信建投隔夜-日内异象**（2025-11）(https://finance.sina.com.cn/wm/2025-11-26/doc-infystcp1980904.shtml) — 负隔夜溢价+32 细分因子+领先-滞后，日线可复现。
- **招商证券行业动量改进与 ETF 落地**（2025-02）(https://www.nxny.com/report/view_5870475.html) — 行业动量失效条件、RankIC 9.67%、ETF 组合 18–25% 年化。
- **华泰 ETF 资金流行业轮动**（2024-10）(https://www.nxny.com/report/view_5777065.html) — 周频年化>20%、胜率~70%。
- **开源证券大小单资金流行业轮动**（2024）(https://www.fhyanbao.com/rpview/1285081) — 主动超大单强度 RankICIR 1.16。
- **方正金工热点反转因子**（2025-03）(https://finance.sina.com.cn/roll/2025-03-13/doc-inepnyni3523740.shtml) — 低关注度方向最新券商实证+凸显效应 2024-09 后失效的警示。
- **东吴证券理想反转因子**（订单簿温度系列）(https://bigquant.com/wiki/doc/4Um8LjwmYA) — 成交笔数切割反转，IR 2.51–2.97。
- **MAX 异象（金融研究 2020）** (http://www.jryj.org.cn/CN/Y2020/V476/I2/167) 与 **RMAX（IRFA 2021）** (https://www.sciencedirect.com/science/article/abs/pii/S1059056021000149) — 彩票异象的 A 股证据与涨跌停修正。
- **T+1 与负隔夜收益**（Economic Modelling 2020）(https://ideas.repec.org/a/eee/ecmode/v89y2020icp55-71.html) — 负隔夜溢价的制度根源。
- **MSCI China A-share factor investing**（2025-12）(https://www.msci.com/research-and-insights/paper/are-you-really-capturing-the-right-factors-unlocking-deeper-insights-in-china-a-share-factor-investing) — A 股因子层级与全球不同（高股息/低波领先）。
- **中证指数 7 因子长期表现**（2010–2024）(http://caifuhao.eastmoney.com/news/20250323214830650058050) — 价值/红利/低波/质量/现金流有效、成长/动量弱，等权优于市值加权。
- **中银国际 A 股风格月报**（2026-06）(http://www.cy-mmm.com/doc-6eb832f79e228e0785004a26b078f23c.html)、**衡泰 2026-06 因子报告** (https://www.xquant.com/xinwendongtai/2026/1729.html)、**Premia Q4 2025 因子回顾** (https://www.premia-partners.com/insight/china-a-shares-q4-2025-factor-review) — 2025–2026 年风格轮动的最新盘面证据。
- **招商证券 PB-ROE/ROE 稳定性** (https://wap.hibor.com.cn/data/f13c4a1e2c1a840a1df94cd4460c3fe7.html)、**东方证券 PB-ROE 策略** (http://msd.microbell.com/data/03d54c759066806eb9789833f5372cc0.html)、**财新智能贝塔价值×质量** (http://index.caixin.com/upload/smartbeta03/caixinsmartbeta0302.pdf) — 质量与价值互补性的业界证据。
- **行业中性化研究**（多空应中性、纯多头不应）(https://bigquant.com/wiki/doc/WcFDXPplAT)、**方正 2024 行业轮动超额 6.57%** (https://www.nxny.com/report/view_5848303.html) — 行业维度落地建议。
- **国泰海通涨停板情绪择时**（2025-05）(https://news.qq.com/rain/a/20250515A09EJF00) — 涨停效应作为情绪层。
- **Anomalies in the China A-share market**（PBFJ 2021）(https://ideas.repec.org/a/eee/pacfin/v68y2021ics0927538x21001141.html) — 残差动量/反转强的交叉验证。
- **华创证券基本面因子收益来源**（2025）(https://www.hibor.com.cn/repinfodetail_4323939.html) — 盈余公告日超额、基本面策略收益机制。

### 丢弃
- **FactorHub 质量/成长因子检验页**（https://factorhub.cn/factors/category/quality 等）— 展示的年化收益为 0.03–0.05% 量级，明显与主流研究矛盾，疑似样本/处理口径问题，数值不可信，仅作因子清单参考。
- **Atlantis Press 会议论文**（行业轮动/股指预测若干篇）(https://www.atlantis-press.com/article/126024813.pdf 等) — 样本区间旧、方法为通用 ML 框架，证据强度低。
- **MBA 智库文档、vocus 自媒体涨停统计** — 非一手来源，仅作背景。
- **2016-2018 年的老单因子测试**（华泰 2016 等）— 结论仍被新报告引用，但本身时效性差，仅作口径参考。
- **东财财富号/自媒体转述**（除中证官方数据外）— 避免二次转述失真。

---

## Gaps（未能确证的问题与下一步）

1. **"成交干旱/低成交异象"的 A 股直接实证证据薄弱**：检索到的直接证据主要是"异常低成交量与负盈余意外相关、可预测盈余公告附近收益"（[MPRA](https://ideas.repec.org/p/pra/mprapa/92162.html)）以及量价趋势、潮汐因子，未找到像美股 Liu et al. (2019) 那样的"成交干旱/低关注度溢价"A 股专项实证。**建议用日线自测代理指标：零收益日占比（zero-return days）、低成交量日频率、Amihud 非流动性、相对 120 日均量的异常缩量，验证与现有流动性/反转因子的正交性。**
2. **券商研报的具体回测区间与净值多为付费内容**：华泰/招商/开源等报告只能获得摘要级数字（年化超额、IC、胜率），无法核实其回测口径（是否含 ST/次新、行业中性化、交易成本）。落地前需自行复现。
3. **行业轮动类因子的数据依赖**：ETF 资金流需 ETF 份额/成交数据、大小单资金流需逐笔或分笔委托数据、北向资金 2024-08 后已停止披露盘中数据（仅披露持股），纯日线数据无法完整复现资金流类因子，只能复现行业动量/强弱/拥挤度部分。
4. **质量/成长因子的可复现性关键在财务数据对齐**：申万报告明确提示财报披露节奏（1–4 月一季报、5–8 月二季报等），必须按"已知信息时点"对齐而非公告后统一取数，否则存在前视偏差；招商 PB-ROE 报告强调"未来 ROE 才知道谁好"——任何 ROE 因子都要处理稳定性/预期成分。
5. **2024–2026 区间内部分因子的时变性**：凸显效应因子 2024-09 后失效、2022 后行业动量衰减、2025Q4 成长→价值切换，说明近年因子收益切换快，任何新因子上线前都建议做滚动窗口（如 12 个月）的 IC 稳定性检验。
6. **拥挤度因子公开实证偏负面**（ML 择时无效），如需拥挤度维度，建议按招商 2025 的做法作为行业/动量因子的惩罚项，而不是独立选股因子。

### 对现有策略的落地建议（按证据强度排序）
- **优先：质量/盈利因子（补财务数据后）**——ROE 稳定性处理 + 毛利率/GPOA + 资产负债率健康检查，作价值因子的互补项；预期对 2021–2023 价值回撤年有对冲。
- **次优：隔夜-日内分解与 MAX 因子**——纯日线（开/收/最高价）可复现，与价值正交性好，与低波/反转需正交化。
- **再其次：行业轮动层**——行业动量（动量择时+拥挤度惩罚）用日线行业指数可复现；资金流类因子等数据源到位后再上。
- **成长因子单列但需二次选股**（叠加行业轮动或低波过滤），不做独立主力因子。
