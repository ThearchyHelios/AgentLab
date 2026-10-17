"""SQL 语义：两条 SQL 算的是不是一回事，不是的话差在哪。

双盲复算要回答「盲算和原流程的 SQL 差在哪」（A 按支付时间筛、B 按下单时间筛；A 排除了作废的行、B 没有），人据此选
A / B，只差一个方面、又对得上数据目录里的事实（时间归属、有效行过滤、去重）时选择会写进数据目录；运行对比要回答
「这一期和上一期的 SQL 是不是同一个模板」（定时运行每次的日期不同，不能算模板变了）；反方审稿要看自己的查询和原
查询的口径是否一致。几处共用这里：

- normalize_sql：字面量换成占位符之后的规范文本，加上字面量本身（按规范文本里的顺序）。两条 SQL 只有字面量不同，
  规范文本就相同；关键字大小写、空白、普通注释、不加引号的名字的大小写都不算不同；结构变了的一定不同——解析时会
  丢掉写法的（MySQL 会执行的 /*! … */ 注释、写成 ! 的 NOT、SQLite 的 MOD() 这类重新生成时换了名字的函数）改按原文
  逐词规范化，会执行的注释里的代码也算结构。
- explain_sql_diff：两条 SQL 是不是等价（SqlDiff.equivalent），不是的话差在哪（items、text_diff）。
- diff_facets：照旧收两份 facets，交给 explain_sql_diff 比两份记下的原文，返回它的 items（含兜底条目）：完全一致
  （规范形式一字不差）才返回空列表。
- facets：把一条查询拆成表、连接、WHERE 合取式、时间条件、分组、聚合、算出来的列、HAVING、取哪几行（给调用方取
  结构用；CTE、子查询按位置编号，见 _Statement）。facets 不受下面等价判定的改动影响。

**等价怎么判。** 过去几轮按方面归纳任意 SQL，每轮修好一批又冒出一批（还回退过），收不拢；现在先判等价、再说差在哪。
两边各自规范化，规范化的每一步都只做保持语义的变换，同一级的两个规范形式一字不差（比语法树逐个节点的类型和参数，
见 _fingerprint，括号不算；不比生成的 SQL：sqlglot 按方言生成时会把不同的写法写成一样，SQLite 的 CAST(x AS DATE)
生成 DATE(x)）才算等价，否则一律不等价、条目一定不为空。原文一字不差的两条直接算等价。规范形式分三级（见
_canonical_forms），哪一级对上都算：

- optimizer：先按方言统一名字（_normalize_names），CTE、子查询的列名表写成里面那段的列别名，USING 写成 ON，FETCH
  FIRST 写成 LIMIT，IS NOT NULL、NOT LIKE 这类带 negate 的写法统一成 NOT (…)；然后 sqlglot 的 qualify 认列（给了
  数据源的 schema_cache 就按它认、展开 SELECT *）；再把整条语句的来源别名改成互不相同的，引用了好几次、只筛行选列的
  CTE 每处写回一份，只引用一次的 CTE 写回用到它的地方，只从几张表筛行选列的子查询并进外层（_merge_derived：只并
  内连接一侧的），外层 WHERE 里只涉及分组子查询选出来的列的条件挪进它的 HAVING（同一个值），去掉没用的 CTE。
- light：不认列，只做上面不需要认列的几步。
- parsed：解析后只统一名字的大小写、引号。

两级共用的几步（见 _finish，各函数的说明写了为什么保持语义）：原样透出子查询全部列的一段换成那个子查询；两个来源
的 RIGHT JOIN 写成 LEFT JOIN；WHERE 把外连接可空一侧没关联上的行筛掉了的写成内连接（_rejects 分「一定是空值」和
「一定不成立」：NOT 只能套前者，NOT IN 子查询、NOT IN ()、x op ALL (…)、NOT (x = ANY …)、PostgreSQL 的 || 都留得下
没关联上的行）；不分组、结果只是跳过空值的聚合、参数在可空一侧为空时一定为空的，LEFT JOIN 写成内连接
（_null_blind_left_joins：COALESCE(SUM(r.amount), 0) 和 INNER JOIN 一样）；ON 之外没用到、结果又不怕重复的行（去重、
只有 MIN / MAX / 去重的聚合、IN / EXISTS 子查询）的 LEFT JOIN 去掉；开头那组内连接的条件挪进 WHERE；WHERE、HAVING、
ON 里的 TRUE、1 = 1、分组后的 COUNT(*) >= 1 去掉（没有 GROUP BY、别处也没有聚合的 HAVING TRUE 留着：PostgreSQL
里它让语句只出一行）；同一组 AND 里另一项已经保证不为空的 x IS NOT NULL 去掉；给了表结构、两列声明的类型一样又「相等
就是一样」时，WHERE 里两列相等的，SELECT / GROUP BY / HAVING / ORDER BY 里都换成同一列（_substitute_equal_columns）；
EXISTS 子查询写成 SELECT 1（去重、分组、排序、取前 n 行、UNION 还是 UNION ALL 都不影响）；IN 子查询里不影响取值集合
的 DISTINCT、GROUP BY、ORDER BY 去掉、UNION 写成 UNION ALL（只在那一列「相等就是一样」时）；SELECT DISTINCT 写成按
选出来的列 GROUP BY、已经按选出来的列分组的 DISTINCT 去掉；COUNT(1) 写成 COUNT(*)、MIN / MAX 里的 DISTINCT 去掉；
SQLite 的 IIF、MySQL 的 IF 写成 CASE，CASE 的 ELSE NULL 去掉；IN 列表去重排序、只有一个常量的写成 =；BETWEEN 写成
两个比较（SYMMETRIC、MySQL 里上下界类型不同或不是常量的不改）；NOT 往里推：套比较写成反过来的比较（ANY / ALL 不动）、
NOT NOT 条件去掉、德摩根律（三值逻辑下照样成立）；LIMIT / OFFSET 的整数算式算成一个数（SQLite、PostgreSQL），SQLite
的负数 LIMIT、PostgreSQL 的 LIMIT NULL 写成不限行数；ORDER BY 的 ASC、OFFSET 0、和前面一模一样的排序键、分组键去掉；
最外层 ORDER BY 引用的别名换成表达式，最外层和条件里的子查询选出来的列名去掉；最后起规范的名字、交换律成立的地方
排好序。

**不靠表结构里的约束改写。** 主键、唯一约束、外键、NOT NULL 一律不拿来改写、判等价：凭键去掉没用到的 LEFT JOIN、
去掉 DISTINCT，把 COUNT(x) 当成 COUNT(*)，凭主键认分组的函数依赖，凭 NOT NULL 去掉 IS NOT NULL，都不做。第 4 轮复验
在这些地方抓到判错的等价：SQLite 的类型亲和（TEXT 主键对 INTEGER 列，'1' 和 '01' 都等于 1，一行关联上两行）、比较
写了 COLLATE NOCASE、部分唯一索引（只管没删的行，探查照样记成 unique）、MySQL 拿字符串和数值比时的隐式转换，都会让
「唯一」不成立，表结构里看不全，宁可认不出。表结构只拿来认列、展开 SELECT *、看列声明的类型（「相等就是一样」、是不是
精确数），约束只拿来让差异说明少说（见下面的具体说法）。

**保真守卫：原文里有 sqlglot 会读错、会丢掉的写法时，不在语法树上判等价（连只统一名字的那一级都不比），也不给具体
说法，只给兜底说明，附原文的逐行对照**（见 _lost_in_parsing、_misread）。按方言切出来的词逐个看、按括号深度分层，
不拿正则硬匹配：

- 读错的：MySQL / MariaDB（以及不知道是哪种库时）字符串里的反斜杠转义（sqlglot 不认 \\Z、\\0，原样留着反斜杠，和
  写成 \\\\Z 的撞成同一个值）；数字后面紧跟着字母、下划线（1_000 是一千、0o17 是 15，sqlglot 切成数字加别名）；两个字符串
  常量挨着写（MySQL 里是拼接，SQLite 里是别名）；同一层括号、同一段条件里比较运算和 IS / IN / LIKE / BETWEEN / 另一个
  比较运算没加括号地混用（a = b IS NULL、a < 100 IN (0, 1)、a = 100 < 200：各方言的结合顺序和 sqlglot 的不一样）；
  按位运算（& | ^ << >> ~，PostgreSQL 的 # 这类）和算术、拼接、另一种按位运算没加括号地混用；写成符号、sqlglot 却当成
  LIKE 那一级来读的运算符（PostgreSQL 的 ~、~*、~~、~~*、@@、-|-……）和别的二元运算混用（PostgreSQL 里它们和 ||、&
  同级、从左往右：status ~ 'p' || 'x' 是 (status ~ 'p') || 'x'，sqlglot 读成 status ~ ('p' || 'x')；写成关键字的
  LIKE / ILIKE 本来就比 || 松，不算）；PostgreSQL 的前缀 @（取绝对值，sqlglot 读成参数）、|/ 和 ||/（双精度的开方，
  sqlglot 写成 numeric 的 SQRT）、COLLATE 后面同一段里的 ::；XOR；不知道是哪种库时的 ||、#、块注释里套着的 /*（PostgreSQL
  嵌套、SQLite / MySQL 不嵌套）、后面紧跟着字的 --（MySQL 里是两个减号）、双引号括起来的名字（MySQL 默认的 sql_mode 里
  是字符串），MySQL 以外的 &&。第 5 轮在真实的
  PostgreSQL 18.6、MySQL 8.0、SQLite 上做过差分压测（不加括号的随机运算符混用对 sqlglot 读出来的树每个操作数都加
  括号，各几千条）：读错了的都在这里拦下。
- 丢掉的：一元加号（SQLite 里 +a 去掉了类型亲和）、0x10 和 x'10' 这类只留下值的字面量、换了名字的类型名、MySQL 会执行
  的注释（/*! … */）、写成 ! 的 NOT；解析时换了写法又没留位置的函数名（见 _mark_surfaces：SQLite 的 MOD() 和 % 解析成
  同一个节点，记下原文写的名字就不会判成等价）。MySQL 里后面不跟空白的 -- 是两个减号，先拆开再解析。

**不走规范化两级的（只比解析后的原样）**：不确定的函数（RANDOM()：两次求值不是同一个值，去重 AND / OR / IN 列表里
的项、并子查询都会改变求值次数）；递归 CTE（SQLite 不写 RECURSIVE 的也是）、LATERAL、表函数、VALUES、实表别名带
列名、ROLLUP 这些结构；子查询里写了表名的列、而这个别名在里外层各指一个不同来源、认不准是哪一个的（SQLite 里层找
不到这一列就往外层找）。optimizer 一级另外不出的：认不全列；WHERE / GROUP BY / HAVING 里、或者套着的子查询里有和
列别名同名、又可能是表的列的名字（SQLite 的 WHERE、ON、相关子查询都认列别名）；FROM 里的子查询、CTE 选出来的列有
同名的。

**故意不做的改写**（复验在这些地方抓到过不保持语义的例子）：sqlglot 的 simplify（数字常量按 Decimal 折叠，SQLite
按浮点数算 0.1 + 0.2 ≠ 0.3；-(-x) 化成 x；BETWEEN SYMMETRIC 丢了 SYMMETRIC）、unnest_subqueries、canonicalize、
pushdown_projections；HAVING 里的条件挪进 WHERE；只有一项、又不是常量的 IN 列表写成 =（SQLite 里 a IN (b) 是
a = +b）；改列别名、把 EXISTS 写成 SELECT 1、去掉列名之前，本段（含 WHERE、ON、相关子查询）还按名字引用这些别名的
（见 _alias_refs）；USING 的列和某个列别名同名的（ORDER BY 里单独一个名字先认列别名）；靠约束的改写（见上）。

**结果看行出来的先后的一段（见 _Sensitivity）不调换来源的先后、不排 GROUP BY 的键、不改 DISTINCT、不并子查询、不把
引用了好几次的 CTE 拆开，也不按 WHERE 把外连接改成内连接、不去掉条件（连一模一样写了两遍的也不去）、不去重排序键和
分组键、不换列、不把 BETWEEN 拆成两个比较、不去掉 MIN / MAX 里的 DISTINCT**：取行（LIMIT / OFFSET / FETCH，有并列时
取哪几行也看先后）、DISTINCT ON、窗口函数、拼串拼数组的聚合（GROUP_CONCAT）、聚合里既不分组也不聚合的列、当成标量用
的子查询，去重、分组、MIN / MAX、去重的 UNION 留下的值「相等却不一样」的时候（MySQL 默认不分大小写的字符串、
PostgreSQL numeric 的 2.0 和 2.00、算出来的表达式），以及 PostgreSQL、MySQL 里对不知道是不是精确数的值求和；给这样
一段供行的子查询、CTE 也算。第 3 轮压测在真实的库上看到：SQLite 里 WHERE 让 LEFT JOIN 降成内连接之后，扫描的先后和
直接写内连接的不一样（EXPLAIN QUERY PLAN 一个先扫 refunds、一个先扫 orders）；MySQL 里同一个条件写两遍，按条件估算
的行数变了、换了关联的先后——GROUP_CONCAT 拼出来的先后都跟着变。

**假设（做不到的）**：SQLite 没给表结构时按列有声明的类型、默认的 BINARY 排序规则算——声明了 COLLATE NOCASE 的列、
没有类型亲和的列（能同时存 1 和 1.0）表结构里看不出来；比较两边都是列时对调两边（SQLite 比较两列用左边那列声明的
排序规则）。SQLite 的求和当成和先后无关。PostgreSQL、MySQL 没给表结构时一律不当成「相等就是一样」、也不当成精确数。
MySQL 的表名按 Linux 上分大小写算；MySQL 按默认的 sql_mode 读：双引号括起来的是字符串、|| 是 OR（开了 ANSI_QUOTES、
PIPES_AS_CONCAT 的库这两处的意思不同，sqlglot 的 MySQL 读法和这里都按默认的算）。

等价的结论另有执行比对守门（tests/test_sql_equivalence.py）：虚构库上五组随机数据、一组极端数据、一组专挑写法差异的
数据，判等价的两条每组结果都得一样，结果不一样的绝不能判等价；复验报过的反例都收在语料里。

**具体说法（见 _Explainer）。** 只在两边的规范形式都是「简单形状」时说（_simple_block：整条语句就是一段 SELECT，没有
CTE、子查询、UNION 这类合并、窗口函数、DISTINCT ON；FROM 里只有实表，连接只有内连接、CROSS JOIN、逗号和 LEFT JOIN；
LIMIT 是至少为 1 的常数；没有认不准的名字、SELECT 列表里没有 sqlglot 不认识的函数；不认列的那一级里原文没有在
WHERE / GROUP BY 里用列别名的），而且只说五类：

- 时间列（time）：两边各在一张底层表的一列上按时间筛、列不同（另一边哪儿都没用这一列筛选，两边的时间条件都不是恒为
  真的，时间列不在 LEFT JOIN 的可空一侧）；一边按时间筛了、另一边哪儿都没有可能是时间窗口的条件的（见下），也说成
  time（「A 按 paid_at 筛时间，B 没有按时间筛选」）。时间条件是一列（可以套着日期函数、CAST）和常量比较、IN、BETWEEN，
  列名像时间或者常量像日期；或者时间列 LIKE 一个像日期的模式（SQLite 里日期存成文字，paid_at LIKE '2026-09%' 是取月）。
  两边的时间列在不同的表上时写成「表.列」。detail 给两边的 [{table, column}]。
  「另一边可能按时间筛了」（_Usage.temporal）认得很宽：条件里（WHERE、ON、HAVING，聚合的参数、CASE、FILTER 里也算）
  用到可能是时间的列（date_key、stat_month、pt、fiscal_year 这类，见 _timeish_name）、日期函数、像日期的常量
  （20260901、202609 这类整数，'2026'、'2026Q3'、'2026-09%'），或者对同一个对象有上下界（BETWEEN、>= 和 < 一对）。
- 多一个筛选条件（filter）：一边多出来、只涉及一张底层表的列和常量比的条件——另一边整条语句（WHERE、ON、HAVING、CASE /
  FILTER 里，聚合的参数里套在别的运算、函数里的：SUM(status = 'paid')、COUNT(… OR NULL)、COUNT(NULLIF(…))、
  SUM(amount * (1 - is_deleted))；判空、或者条件只筛掉空值时连光一列的聚合参数）对这些列没有任何条件；这一边对这些列
  的条件全是另一边没有的（不是被另一边也有的条件蕴含的）；比的对象就是那一列（不是 LOWER(x) 这类认不出取值范围的）；
  按取值逐段算过不是恒为真的（_Range.always：表结构里确定不为空、又不在真的可能整行为空的外连接一侧的列不取空值）；
  不用到 LEFT JOIN 可空一侧的列（WHERE r.id IS NULL 是反连接，说的是「没关联上」；r.order_id IS NULL 和 ON 里的
  r.order_id = o.id 一起已经蕴含 r.id IS NULL）；不是可能的时间窗口（_window_like：date_key BETWEEN 20260901 AND
  20260930、stat_month = 202609——认不准是不是时间列，说成有效行过滤会被写进数据目录）。detail 给条件涉及的实表列。
- 同一列上的条件改写（filter / time_range，a、b 两边都有）：两边 WHERE 里对同一张实表的同一列各写了一套条件、留下的
  取值不同——等号对 IN（口径 status = 'paid'，这次 status IN ('paid', 'refunded')）、取值集合不同、范围的端点不同，说
  「A 对 status 的条件是 …，B 是 …」，两边都只是取值集合、又有共同的取值时补「（B 多了 'refunded'）」。要两边用到这一列
  的条件都只涉及这一列、比的就是那一列本身，按取值逐段算过两边留下的取值确实不同（_Range.compare：表结构里确定不为空
  的列不拿空值比；x >= 100 AND x >= 50 对 x >= 100 这类冗余写法算出来一样，不说），这一列不在 LEFT JOIN 的可空一侧，
  两边在 WHERE 之外（ON、HAVING、CASE / FILTER、聚合的参数）都没用到这一列，不是认不准的时间窗口，没换表。两边都是
  时间条件时 aspect 是 time_range。只改说明：走到这里时已经判了不等价（见 _Explainer._rewrites）。
- 聚合与去重（aggregate / group_by）：选出来的列只差在顶层的聚合函数上（同一列做参数，SUM 对 AVG；COUNT(*) 对 SUM(x)）
  或者去重上（COUNT / SUM / AVG 去不去重、COUNT(*) 对 COUNT(DISTINCT x)、SELECT DISTINCT 对不去重），分组一样。参数
  得是光一列、在每组里不是定死的（不被 WHERE 定成常量、不等于分组键、表结构里它那张表的键没有在组里定死——按 u.id 分组
  时 MAX(u.city) 和 MIN(u.city) 一样，LEFT JOIN 把行复制了也一样）；去重不起作用的不说（MIN / MAX；不分组的聚合只出
  一行；已经按选出来的列分组；只取一行；表结构里的键说明每行本来就不一样）；每组只有一行时 SUM、AVG、MIN、MAX 不说。
- 关联的表、关联类型（tables / join）：换了一张表；一边多关联了一张表（表结构里有指向它的外键、关联条件又用到它的键的
  不说）；一边多 LEFT JOIN 了一张表（在 ON 之外没用到、结果看得见重复的行、表结构里这张表的键不在 ON 里）；同一张表、
  同样的条件，LEFT JOIN 对内连接（可空一侧在 WHERE、HAVING、后面的连接里都没用到——后面的内连接引用可空一侧时没关联上
  的行反正留不下来；结果看得见没关联上的那些行，不是只取可空一侧跳过空值的聚合、FILTER；原文写的是 RIGHT JOIN、或者
  被 WHERE / 聚合改成内连接的，说法和原文对不上，不说）。另一边原文也 LEFT JOIN 了这张表、只是规范化因为结果不怕重复
  把它去掉了的，不说「另一边没有」。

每一句都先确认这处不同真会影响结果：两边的 WHERE、HAVING、ON 里有可能恒为假的条件（常量条件，同一个对象上取不到值的
几个条件）时什么都不说；结果看不见行的（SELECT COUNT(*) > -1 这类只出一行、值又和行无关的）不说筛选、关联上的不同；
表结构里的约束（主键、唯一约束、外键、NOT NULL）说明不起作用的不说——约束只拿来少说，不拿来判等价。被 WHERE 用常量定死
的列出现在排序、分组里的差异，恒为真的 HAVING，都不在五类里，不说。两边给人看的写法一样、和已经说过的一句正好反过来的
不说。

**兜底。** 规范形式不一致、一条具体说法都没有时，给一条 aspect="other" 的「两条 SQL 的写法不同，未能归纳成具体差异，
请对照原文」；具体说法只覆盖了一部分时追加「另有未能归纳的差异，请对照原文」；fallback 为真。text_diff 给出规范化后
不同的行（difflib 比两边 sqlglot pretty 的输出，WHERE、ON、HAVING 的合取式每个条件一行，不同的行前面带上标出所在子句的
上级行，最多 40 行，见 _text_diff）；有一边没有规范形式（保真守卫拦下的）时给原文的逐行对照。

仍做不到的（只给兜底说明，或者认不出等价）：
- 认不出的等价改写：要靠约束才成立的（主键不为空时的 COUNT(主键) 和 COUNT(*)、按主键关联又没用到的 LEFT JOIN、带
  主键的 DISTINCT、按主键分组时的函数依赖）；IN 子查询和 EXISTS、NOT IN 和 NOT EXISTS 互换（空值时本来就不等价）、
  IN / EXISTS 子查询和关联同一张表再去重、条件挪进相关子查询、IN 和 OR、冗余的条件、COALESCE 和 CASE 这类写法不同的
  等价表达式、窗口函数的 PARTITION BY 换先后、HAVING 里分组键上的条件和 WHERE、去重的 UNION / INTERSECT 调换各支、
  SQLite 里套在子查询里的 UNION、三项以上的加乘换结合、和方言有关的同义写法（DATE(x) 和 CAST(x AS DATE)、不在
  _SAFE_RESPELLINGS 里的函数别名）、各支互不相交的 UNION ALL 和 UNION、只出一组时的排序、被 WHERE 定成常量的排序键
  和分组键、恒为真的 HAVING、外层不在乎的子查询里的不同、LATERAL、递归 CTE、实表别名带列名、USING 用在第一个连接以外的
  地方；保真守卫拦下的写法；没给表结构时 WHERE、GROUP BY、HAVING、相关子查询里用列别名的；结果看行出来的先后的一段调换
  关联先后、按 WHERE 把外连接改成内连接、换成相等的列；没给表结构（或者类型不一样）时按关联条件相等的两列换着分组、
  排序；PostgreSQL、MySQL 没给表结构时去重、分组、求和的一段调换关联先后、并子查询、去掉没用到的 LEFT JOIN、把只取
  可空一侧聚合的 LEFT JOIN 写成内连接。
- 只给兜底的：不是简单形状的（CTE、子查询、UNION、窗口函数，规范化没把它们并掉的）；五类以外的不同（算出来的值上的
  条件改写——LOWER(status) = 'paid' 对 LOWER(status) IN (…)、和别的列交错的同一列条件、关联条件、分组、HAVING、排序、
  取几行、选出来的列、聚合的参数）；几张表、几列交错的条件；
  自己关联自己、同名的表关联了几次的；换了表又有关联条件不同的；条件写在聚合里的一边对条件写在 WHERE 里的一边（SUM(status
  = 'paid') 对 COUNT(*) … WHERE status = 'paid'：真正的不同是计数为 0 的组、空集时是 0 还是 NULL，说不清）；反连接；
  可能是时间窗口、又认不准是不是时间列的条件（业务日期键、统计月份、分区，第几天这类范围）。
- 已知的漏洞（只会说出「真会影响结果」以外的话，不会判错等价）：几列交错的矛盾条件（a > b AND b > a）认不出恒为假，
  两边都带着它时别处的不同其实看不出来；结果看不看得见行只按「SELECT 列表里有没有一项本身就是聚合」粗看，CASE WHEN
  COUNT(*) > 0 THEN 1 END 这类恒为一个值的表达式不算聚合，认作看不见（少说），可 MAX(x) 旁边并排的那一项恒为一个值时
  仍按看得见说；表结构里没写外键、没写键的，按「可能对不上、可能重复」说（库里实际有约束时这句话可能不起作用）；没给
  表结构时按主键分组的 MAX(x) 对 MIN(x)、AVG(DISTINCT x) 对 AVG(x) 照说（那时不知道键）；「可能是时间」只认列名里的
  词和像日期的常量，一边按 id 的单边范围、按批次号这类筛出来的其实是一段时间时，另一边仍会被说成「没有按时间筛选」；
  存成 Unix 秒的时间列（created >= 1788220800）认不出是时间，会说成有效行条件；列名带 time 的时长列（wait_time <
  3600）会被说成「按它筛时间」；被 CURRENT_DATE、DATE('now', …)、CAST(…)、上下界相同的 BETWEEN 定死的列不算定死，
  MAX(stat_date) 对 MIN(stat_date) 这类仍会照说。这几类只影响说明的准确度，不会判错等价；双盲复算写数据目录时另有一道
  门槛（业务日期只在两边的列都确实是日期 / 时间类型时才写，有效行条件只收排除无效记录的写法），不会因此写进错的事实。
- 收窄换来的代价：一边的聚合参数里用到某一列（SUM(amount - discount)），另一边多一个这一列上的条件（WHERE discount
  > 0）时，这个条件不再说成「另一边没有这个条件」（认不出参数里的运算是不是在筛）；列名带 day / month / period 这类
  词、常量像日期的条件（delivery_days < 3、period_type = 'monthly'）不再说成有效行过滤；另一边有任何上下界（amount
  BETWEEN 10 AND 100）时不再说它「没有按时间筛选」。都只给兜底。

**和 sqlcheck 一样宁可漏报。** 解析不了（语法错、方言特性、多条语句、不是查询）就只给一条 aspect="parse"；从不抛异常，
规范化或说明本身出了意外就当作这一级没有、说不清，记日志。只读语法树，不执行 SQL。
"""
from __future__ import annotations

import contextlib
import hashlib
import itertools
import json
import logging
import math
import re
from collections import deque
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass, replace
from typing import Any

from app.data import catalog
from app.data.names import name_key
from app.data.sqlcheck import _bare_column, _conjuncts, _parents, _unparen, dialect_of

logger = logging.getLogger(__name__)

#: 规范文本里字面量的占位符
PLACEHOLDER = "?"
#: 太长的 SQL 不做语法分析（normalize_sql 退回逐词规范化，facets 带 parse_error）：解析耗时随长度增长
MAX_SQL_CHARS = 100_000
#: 每段 SELECT 在语法树上记段号的 meta 键。条件、表达式要复制一份再改写，复制出来的子查询靠段号找回原来那一段
#: 算好的来源（别名、外层）：复制品没有父节点，自己找不到外层
_SEGMENT = "sql_semantics_segment"
#: 派生来源（引用 CTE 的表和 CTE 的定义、FROM / JOIN 里的子查询）在语法树上记比较用编号的 meta 键（见
#: _Statement._number_derived）。复制出来的条件里套着子查询时，靠它把 CTE 名、子查询的别名换成编号
_DERIVED = "sql_semantics_derived"
#: 不写编号的写法（render 的 anon 模式）里派生来源一律写成这个：排比较的两边谁在前时先看实表的列还是派生来源的列
_ANON = "@"
#: render 里比较用的写法（见 render）：名字按 name_key，别名、各自起的名字都去掉
_COMPARING = frozenset({"key", "loose", "anon", "shape", "pierce"})
#: 外连接写在 WHERE 里会把可空那一侧没关联上的行（那一侧的列都是空）筛掉的条件：拿空值比都不成立
_NULL_REJECTING = ("EQ", "NEQ", "GT", "GTE", "LT", "LTE", "Like", "ILike", "Between", "In")


# ==========================================================================
# 解析
# ==========================================================================


def _read(dialect: str | None) -> str | None:
    """数据源类型（sqlite / postgresql / mysql …）或 sqlglot 的方言名 → 解析用的方言。认不出的按 sqlglot 的通用读法。"""
    found = dialect_of(dialect)
    if found or not dialect:
        return found
    from sqlglot.dialects.dialect import Dialect

    name = dialect.strip().lower()
    try:
        Dialect.get_or_raise(name)
    except Exception:  # noqa: BLE001 - 不认识的方言名：按通用读法
        return None
    return name


def _parse_problem(error: Exception) -> str:
    """sqlglot 的报错 → 一句给人看的话。原文是英文、还带终端的高亮控制符，只取出错的位置。"""
    errors = getattr(error, "errors", None) or []
    first = errors[0] if errors and isinstance(errors[0], dict) else {}
    line, col = first.get("line"), first.get("col")
    if isinstance(line, int) and isinstance(col, int):
        return f"SQL 无法解析（第 {line} 行第 {col} 列附近）"
    return "SQL 无法解析"


def _parse(sql: Any, read: str | None) -> tuple[Any, str | None]:
    """(语法树, None) 或 (None, 为什么分析不了)。只认单条查询。"""
    import sqlglot
    from sqlglot import exp

    if not isinstance(sql, str) or not sql.strip():
        return None, "SQL 为空"
    text = sql.strip()
    if len(text) > MAX_SQL_CHARS:
        return None, f"SQL 超过 {MAX_SQL_CHARS} 个字符，没有做语法分析"
    try:
        statements = [s for s in sqlglot.parse(text, read=read) if s is not None]
    except Exception as e:  # noqa: BLE001 - 语法错、方言特性、sqlglot 不支持的写法
        return None, _parse_problem(e)
    if not statements:
        return None, "SQL 为空"
    if len(statements) > 1:
        return None, f"一次只能分析一条 SQL，这里有 {len(statements)} 条"
    if not isinstance(statements[0], exp.Query):
        return None, "不是查询语句（只分析 SELECT、WITH 开头的查询）"
    return statements[0], None


# ==========================================================================
# 规范化：字面量换成占位符
# ==========================================================================


@dataclass(frozen=True)
class NormalizedSql:
    """规范文本和字面量。text 相同就是同一个模板，literals 是这一次填进去的值（SQL 写法，字符串带引号）。

    parse_error 不为空时 text 是逐词规范化的结果（关键字转大写、不加引号的名字转小写、字面量换成占位符）：
    同一个模板只改了字面量的两条 SQL 照样得到同一个 text，只是空白、括号之外的写法差异认不出来。
    """

    text: str
    literals: tuple[str, ...] = ()
    parse_error: str | None = None

    def same_shape(self, other: "NormalizedSql") -> bool:
        """两条 SQL 是不是同一个模板（只有字面量不同也算）。"""
        return self.text == other.text

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"text": self.text, "literals": list(self.literals)}
        if self.parse_error:
            out["parse_error"] = self.parse_error
        return out


def _is_literal(token: Any) -> bool:
    """字符串（含 N'…'、x'…'、$$…$$ 这些变体）和数字。"""
    name = token.token_type.name
    return name == "NUMBER" or name.endswith("STRING")


def _tokens(text: str, read: str | None) -> list[Any]:
    from sqlglot.dialects.dialect import Dialect

    return list(Dialect.get_or_raise(read).tokenize(text))


def normalize_sql(sql: str, dialect: str | None = None) -> NormalizedSql:
    """字面量换成占位符后的规范文本 + 字面量。

    先按方言解析、重新生成一遍（去掉注释、统一关键字和空白、按方言的规则转换不加引号的名字的大小写），再把生成的
    文本切成词，字符串和数字换成「?」：字面量按规范文本里出现的顺序排，不受语法树里子句存放顺序的影响。解析
    不了就直接对原文逐词规范化，带上 parse_error。解析时会丢掉写法的（MySQL 会执行的 /*! … */ 注释、写成 ! 的 NOT、
    SQLite 的 MOD() 这类重新生成时换了名字的函数，见 _lost_in_parsing）也对原文逐词规范化（不带 parse_error）：
    重新生成会把不同的结构写成一样。MySQL 里后面不跟空白的 -- 是两个减号，先拆开（见 _mysql_dashes）。从不抛异常。
    """
    read = _read(dialect)
    if read == "mysql" and isinstance(sql, str):
        sql = _mysql_dashes(sql)
    root, problem = _parse(sql, read)
    if root is not None and (_lost_in_parsing(sql.strip(), read, root) or _respelled(sql.strip(), read, root)):
        # 解析时会丢掉写法的（MySQL 会执行的注释、!、SQLite 的 MOD()……）：重新生成的文本会把不同的结构写成一样，
        # 改按原文逐词规范化（会执行的注释里的代码也算进去）
        return _by_tokens(sql, read, None)
    if root is not None:
        try:
            from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

            canonical = normalize_identifiers(root.copy(), dialect=read).sql(dialect=read, comments=False)
            pieces: list[str] = []
            literals: list[str] = []
            pos = 0
            for token in _tokens(canonical, read):
                if _is_literal(token):
                    pieces += [canonical[pos:token.start], PLACEHOLDER]
                    literals.append(canonical[token.start:token.end + 1])
                    pos = token.end + 1
            pieces.append(canonical[pos:])
            return NormalizedSql("".join(pieces), tuple(literals))
        except Exception:  # noqa: BLE001 - 生成、切词出错：退回逐词规范化，照实标出
            logger.warning("SQL 规范化失败，退回逐词规范化", exc_info=True)
            problem = "SQL 的写法无法规范化"
    return _by_tokens(sql if isinstance(sql, str) else "", read, problem)


def _by_tokens(text: str, read: str | None, problem: str | None) -> NormalizedSql:
    """逐词规范化（解析不了的 SQL、解析时会丢掉写法的 SQL）：关键字转大写，不加引号的名字按 name_key，带引号的名字
    原样，字面量换成占位符；! 和 NOT 各写各的。MySQL 会执行的注释（/*!版本号 … */）照样逐词规范化后写进去：里面是
    要执行的代码，版本号算结构（决定执不执行），里面的字面量照样换成占位符。普通注释不算。"""
    try:
        tokens = _tokens(text, read)
    except Exception:  # noqa: BLE001 - 引号、括号没配对：只能把空白压成一个
        return NormalizedSql(" ".join(text.split()), (), problem or "SQL 无法解析")
    words: list[str] = []
    literals: list[str] = []
    glued = False
    for i, token in enumerate(tokens):
        raw = text[token.start:token.end + 1]
        if glued:
            # 数字后面紧跟着的名字（1_000 切成 1 和 _000、0o17 切成 0 和 o17）：和前面的数字是一个字面量
            literals[-1] += raw
            glued = False
            continue
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if token.token_type.name == "NUMBER" and nxt is not None and nxt.start == token.end + 1 and \
                nxt.token_type.name in ("VAR", "NUMBER"):
            glued = True
        if _is_literal(token):
            words.append(PLACEHOLDER)
            literals.append(raw)
        elif token.token_type.name == "VAR":
            words.append(name_key(token.text))
        elif token.token_type.name == "IDENTIFIER":
            words.append(raw)
        else:
            words.append(raw if token.token_type.name == "NOT" and token.text == "!" else token.text.upper())
        for comment in token.comments or []:
            found = re.match(r"\s*(M?!)(\d*)(.*)", comment, re.S)
            if found is None:
                continue
            inner = _by_tokens(found.group(3), read, None)
            words += [f"/*{found.group(1)}{found.group(2)}", inner.text, "*/"]
            literals += inner.literals
    return NormalizedSql(" ".join(words), tuple(literals), problem)


# ==========================================================================
# 各个方面
# ==========================================================================

#: 一列：(表, 列)。表是给人看的真实表名（CTE、子查询写它的名字），认不出是哪张表时为 None
ColumnRef = tuple[str | None, str]


@dataclass(frozen=True)
class Part:
    """一个条件、一个分组键、一个聚合参数……

    key 用来比较：别名换成真实表名，CTE、子查询写位置编号（见 _Statement._number_derived），表名、列名按
    name_key，等式和比较的两边排好顺序；loose 同 key 但不写实表的表名（派生来源的编号照写：它不是表名，换了表也还是
    那一个），两边查的表换了一张（orders 换成 orders_archive）时拿它
    比其余各方面，免得同一个条件只因表名不同被报成不同。text 给人看：好几张表都有的列写「表.列」（真实表名，不是
    别名），其余只写列名；full 一律写表名，两边的 text 一样、key 却不同时改用它说明。excludes 只在 WHERE 的「排除」
    写法上有：<>、NOT、NOT IN、IS NOT NULL 去掉否定之后排除的是什么（IS NOT NULL 写成「列 为空」）。column_keys 和
    columns 一一对应，是各列比较用的写法（实表.列；CTE、子查询的列写成「编号.列」）：两边对同一列的不同条件靠它配成
    一对说。相关子查询里引用外层的列，比较用的写法（key、loose、column_keys）在列名后面带「^」（往外几层带几个，见
    _Statement._depth）：内外读同一张表时，c.parent_id = o.id 和 c.id = o.parent_id 才分得开。

    scope 是这一项在哪一层（条件、分组键、HAVING 上有，见 _Statement._layers）：空串是最外层，「|2」是最外层 UNION
    的第 2 支，「@3」是派生来源 @3 里（「@3|2」是它的 UNION 第 2 支），「?」是条件、SELECT 列表里的子查询。facets 按
    （scope, key）去重：同一个条件在外层和子查询里各写了一次是两项。

    后面四项不进比较（两个 Part 相等只看前面几项），只用来把不同说清楚：
    origins 是这一项用到的派生来源的列各自的来历 [(「t1.total」, 「SUM(orders.amount)」)]（见 _Statement._origin）：
    两边写法一模一样（都是 t1.total > 100），各自的 t1 却不是同一个时，带上它才分得清。pierce 是一路透过派生来源写到
    实表的 key，只在中途被挡住时才有（这一项按派生来源的列写，见 _Statement.through）；stage 说是被什么挡住的（「取 t
    的前 10 行」「算 t 的窗口函数」）：两边 pierce 一样、只有一边被挡住，是同一个条件筛的先后不同。roles 是用到的自己
    关联自己的别名各是哪一个 [(别名, 在关联条件里出的列, 来源的名字)]（见 _Statement._roles）：关联的方向反了，两边都
    写 a.amount，各自的 a 却不是同一个时，带上它才分得清。
    """

    key: str
    text: str
    columns: tuple[ColumnRef, ...] = ()
    excludes: str | None = None
    loose: str = ""
    full: str = ""
    column_keys: tuple[str, ...] = ()
    scope: str = ""
    origins: tuple[tuple[str, str], ...] = field(default=(), compare=False)
    pierce: str = field(default="", compare=False)
    stage: str = field(default="", compare=False)
    roles: tuple[tuple[str, str, str], ...] = field(default=(), compare=False)

    @staticmethod
    def plain(key: str, text: str) -> "Part":
        """没有列的一项（表名、COUNT(*) 的「*」）：比较用的写法都是 key，给人看的都是 text。"""
        return Part(key=key, text=text, loose=key, full=text)


@dataclass(frozen=True)
class JoinFacet:
    """一次连接。kind：inner / left / right / full / cross / semi / anti / natural。table 是关联进来的那一侧，other
    是另一侧：关联条件里出现的别的表（给人看的真实表名）；没有条件的（CROSS JOIN）是在它之前出现的各个来源。on 是
    关联条件的合取式排好序后用 AND 连起来的写法，没有条件是 None。

    sides 是这次连接涉及的来源（比较用的写法，排好序）：关联进来的那一侧，加上关联条件里直接引用的这一段的来源（不
    透过 CTE 找实表）；没有条件的加上它之前出现的各个来源。内连接、CROSS JOIN 两边换个先后意思不变（FROM paid JOIN
    refunded 和 FROM refunded JOIN paid），比它们时按 sides 和条件比，不按哪一侧是关联进来的。

    keep、nullable 只在 LEFT / RIGHT JOIN 上有：保留全部行的主表那一侧、关联不上时记空的那一侧（比较用的写法，排好
    序）。LEFT JOIN 关联进来的那一侧可空，关联条件里的其余来源（没有就是它之前的各个来源）是主表；RIGHT JOIN 反过来。
    orders LEFT JOIN refunds 和 refunds RIGHT JOIN orders 是同一回事，和 refunds LEFT JOIN orders 不是：按主表比，
    不按写的是 LEFT 还是 RIGHT。rejects 是同一段 WHERE 里把可空那一侧没关联上的行筛掉的条件（那一侧的列
    拿来比较、IN、LIKE……，空值都不成立）：有它这次外连接实际等于内连接。scope 是这次连接在哪一层（见 Part.scope）：
    外层关联了 refunds，和 EXISTS 子查询里同样关联了 refunds，是两次连接。"""

    kind: str
    table: Part
    on: Part | None = None
    other: tuple[str, ...] = ()
    sides: tuple[str, ...] = ()
    keep: tuple[str, ...] = ()
    nullable: tuple[str, ...] = ()
    rejects: Part | None = None
    scope: str = ""


@dataclass(frozen=True)
class TimePredicate:
    """WHERE 里的一个时间条件。op：= / <> / > / >= / < / <= / between / in。value 是另一侧给人看的写法：
    BETWEEN 是「低 AND 高」，IN 是「(…)」。

    column 是按哪一列筛时间：条件写的是 CTE、子查询算出来的列（WITH t AS (SELECT DATE(paid_at) AS d …) … WHERE
    d >= …）时，它在里面是同一列套着日期函数、CAST、MIN / MAX 的，换成里面那一列，一直换到实表（paid_at）：按支付
    时间还是下单时间筛，日期表达式写在 CTE 里也认得出。via 是路过的派生来源的列（「编号.列」，不进比较）：时间那一面
    已经报了按哪一列筛不同，派生来源那一列的定义不同就不再另报一遍。table 是这一列一直换到实表的 (表, 列)（不进
    比较）：读同一张表的几个派生来源同在一段时，column 照写派生来源的列（已付那份的 created_at、未付那份的 created_at
    是两列，见 _Statement._seal），给调用方对照数据目录的还是 orders.created_at。"""

    column: Part
    op: str
    value: str
    condition: Part
    via: tuple[str, ...] = field(default=(), compare=False)
    table: ColumnRef | None = field(default=None, compare=False)


@dataclass(frozen=True)
class Aggregate:
    """一个聚合。func 是函数名的小写（sum / count / avg / min / max …）；arg 是参数，COUNT(*)、COUNT(1) 的 arg
    是「*」，去重聚合是 DISTINCT 里面的表达式；text 是整个聚合给人看的写法。窗口函数不算（它们按所在的那一列比，见
    Computed）。"""

    func: str
    arg: Part
    distinct: bool
    text: str
    scope: str = ""                 # 在哪一层（见 Part.scope）：外层算的和标量子查询里算的同一个聚合是两项


@dataclass(frozen=True)
class Computed:
    """选出来的一列是怎么算的。两种：

    - 最外层（UNION 的各支）选出来的列，不是光一列（分组那一面比）、也不是字面量的，比如 SUM(p.total) - SUM(r.total)、
      SUM(amount) OVER (PARTITION BY user_id)、CASE …；ROUND、CAST、COALESCE 给默认值这类外壳先剥掉（见
      _unwrap_value）。光一个聚合也收：另一边可能拿同一个合计再算了一下（SUM(amount) 对 SUM(amount) / 100）。scope、
      scope_key 为 None，name 为空串。
    - 派生来源（CTE、FROM 里的子查询）里被外面用到、又不是原样透出一列的列：它在里面的定义（amount * 0.9、
      DATE(paid_at)、ROW_NUMBER() OVER (…)、p.total - r.total）。外面按「t.net」引用它，两边写的都是 t.net，算法却可能
      不同，只比外面的写法看不出来。scope 是派生来源给人看的名字，scope_key 是它的位置编号，
      name 是列名的 name_key，shown 是列名的原样写法。

    expr 是定义（按它那一段的来源写的 Part；UNION 的各支用「|」连起来）。template 是把里面的聚合换成占位符之后的写法、
    aggregates 是里面的聚合（按出现的先后）：两边只差在聚合上（SUM(amount) * 100 对 SUM(paid_amount) * 100）、聚合
    那一面又已经报了，这里不再报一遍；聚合的集合一样、只是位置换了（SUM(a) - SUM(b) 对 SUM(b) - SUM(a)）才在这里报。
    """

    scope: str | None
    scope_key: str | None
    name: str
    shown: str
    expr: Part
    template: Part
    aggregates: tuple[Aggregate, ...] = ()


@dataclass(frozen=True)
class LimitFacet:
    """一段查询取哪几行。count、offset 是行数和跳过行数的写法（没写为 None，OFFSET 0 也算没写）；order 是这一段的
    排序键 (Part, 是否降序)：只取前几行时，按什么排决定了取到的是哪几行。scope 是哪一段：None 是最外层，CTE 写它的
    名字，FROM 里的子查询写它的别名（没有别名写「子查询」）。条件里套着的子查询不单算：它整个写在那个条件里比。
    scope_key 是这一段所属派生来源的编号（和 Part.key 里的同一套编号；最外层、UNION 的某一支这类不是派生来源整段的
    为 None）。"""

    count: str | None
    offset: str | None = None
    order: tuple[tuple[Part, bool], ...] = ()
    scope: str | None = None
    scope_key: str | None = None


@dataclass(frozen=True)
class SetOpFacet:
    """一段查询把几支 SELECT 合起来（UNION、UNION ALL、INTERSECT、EXCEPT）。ops 是各处合并的写法，按从左到右的先后
    （「UNION」「UNION ALL」…）；branches 是一共几支。scope 是哪一段：None 是最外层，CTE、FROM 里的子查询写它的名字；
    scope_key 是那一段所在的层（见 Part.scope：最外层是空串，派生来源是它的位置编号）。条件里的子查询不单算：
    它整个写在那个条件里比。"""

    ops: tuple[str, ...]
    branches: int
    scope: str | None = None
    scope_key: str = ""


@dataclass(frozen=True)
class Facets:
    """一条查询的各个方面。每一项都按在 SQL 里出现的先后排、按 key 去重；parse_error 不为空时其余都是空的。

    limit 是最外层 LIMIT 的行数（写法）；limits 是各段取哪几行（最外层在前，见 LimitFacet），只有写了 LIMIT 或
    OFFSET 的段才有。

    sources 是各段 FROM / JOIN 里的来源（实表、CTE、子查询；key 是比较用的写法，派生来源是编号，text 是给人看的
    名字），按出现的先后、按 key 去重；条件里套着的子查询（IN (SELECT … FROM users)）的来源不算，它们只筛行，不是
    连接的一头。外连接的主表、可空那一侧按它写名字。

    expressions 是算出来的列（见 Computed）。derived 是用到了的派生来源（key 是编号，text 是名字，含条件里套着的子
    查询用到的 CTE）：两边写法一样、同名的派生来源却不是同一个时，拿它说清「盲算的 t1 对应原流程的 t2」。

    where、group_by、having、joins、aggregates、time 的每一项都记着在哪一层（Part.scope、JoinFacet.scope、
    Aggregate.scope），按（层, key）去重；scopes 是这条语句里有的各层（key 是层的标记，text 是给人看的说法：「外层」
    「t」「t 的 UNION 第 2 支」「条件的子查询」），给人说是哪一层时用。group_by 里也有当分组用的
    SELECT DISTINCT 选出来的各项（这一段没有 GROUP BY、没有聚合时，和按它们分组出的行一样），distinct 是这样收进来的
    那几层：说「去重」不说「分组」。set_ops 是各段的 UNION、INTERSECT、EXCEPT（见 SetOpFacet）。grain 是最后出来
    的行按什么分（key 为「group:分组键…」「one」「rows」，见 _Statement._grain；最外层是 UNION 的为 None）。

    只收用到了的段：没用到的 CTE（定义了、外面没引用）里的条件、聚合、表不影响结果，不收（见 _Statement._enliven）。

    _origin 是 facets 记下的 (原文, 表结构)，不进比较、不进 as_dict：diff_facets 拿它交给 explain_sql_diff 比原文。
    """

    dialect: str | None = None
    tables: tuple[Part, ...] = ()
    joins: tuple[JoinFacet, ...] = ()
    where: tuple[Part, ...] = ()
    time: tuple[TimePredicate, ...] = ()
    group_by: tuple[Part, ...] = ()
    aggregates: tuple[Aggregate, ...] = ()
    having: tuple[Part, ...] = ()
    limit: str | None = None
    limits: tuple[LimitFacet, ...] = ()
    sources: tuple[Part, ...] = ()
    expressions: tuple[Computed, ...] = ()
    derived: tuple[Part, ...] = ()
    set_ops: tuple[SetOpFacet, ...] = ()
    distinct: tuple[str, ...] = ()
    grain: Part | None = None
    scopes: tuple[Part, ...] = ()
    parse_error: str | None = None
    _origin: tuple[str, Mapping[str, Any] | None] | None = field(default=None, compare=False, repr=False)

    def as_dict(self) -> dict[str, Any]:
        return _jsonable(self)


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in fields(value) if not f.name.startswith("_")}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def facets(sql: str, dialect: str | None = None, *, schema: Mapping[str, Any] | None = None) -> Facets:
    """把一条查询拆成各个方面。从不抛异常：解析不了、或者分析时出了意外，返回带 parse_error 的 Facets。

    schema 是数据源的 schema_cache 形状（{"tables": {表名: {"columns": [{"name": …}]}}}），可以不给。给了的话，
    几张表连在一起、列又没加表前缀时按它认列是哪张表的；不给就照写列名，比较时可能把同一列当成两列。

    返回的 Facets 记着原文和表结构（_origin）：diff_facets 拿它交给 explain_sql_diff 比原文。
    """
    read = _read(dialect)
    root, problem = _parse(sql, read)
    if root is None:
        return Facets(dialect=read, parse_error=problem)
    try:
        return replace(_Statement(root, read, schema).facets(), _origin=(sql, schema))
    except Exception:  # noqa: BLE001 - 分析本身的缺陷不能连累调用方：当作分析不了，记下来修
        logger.warning("SQL 语义分析失败", exc_info=True)
        return Facets(dialect=read, parse_error="SQL 的结构无法分析")


# ---- 语法树上的小工具

#: 比较：(运算符的写法, 两边对调之后的类型)
_COMPARE: dict[str, tuple[str, str]] = {"EQ": ("=", "EQ"), "NEQ": ("<>", "NEQ"), "GT": (">", "LT"),
                                        "GTE": (">=", "LTE"), "LT": ("<", "GT"), "LTE": ("<=", "GTE")}
#: 日期、时间函数（sqlglot 把各方言的写法归成这些类型：strftime、DATE_FORMAT 都是 TimeToStr）
_DATE_FUNCS = ("CurrentDate", "CurrentDatetime", "CurrentTimestamp", "CurrentTime", "Date", "Datetime", "DateAdd",
               "DateSub", "DateDiff", "DateTrunc", "TimestampTrunc", "DatetimeTrunc", "TimeToStr", "StrToDate",
               "StrToTime", "TsOrDsToDate", "TsOrDsToTimestamp", "TsOrDsAdd", "DateStrToDate", "TimeStrToTime",
               "UnixToTime", "Interval", "Extract", "DateFromParts", "LastDay", "Year", "Month", "Day", "Week",
               "Quarter", "DayOfWeek")
#: sqlglot 不认识、原样当普通函数的日期函数名
_DATE_NAMES = frozenset({"date", "datetime", "strftime", "julianday", "now", "today", "to_date", "to_char",
                         "date_format", "unixepoch", "curdate", "sysdate", "getdate"})
#: 像日期、时间的字符串：2026-09-01、2026-09、2026/9/1、2026-09-01 08:00:00
_DATE_TEXT = re.compile(r"^\s*\d{4}[-/]\d{1,2}(?:[-/]\d{1,2})?(?:[ T]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?\s*$")
#: 中文列名里像日期、时间的写法（英文列名按 catalog.is_date_column）
_CN_TIME = re.compile(r"(?:日期|时间|时刻|年月|月份)$|^(?:年|月|日|周|日期|时间)$")
_PLAIN_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _nested(node: Any, stop: Any) -> bool:
    """node 在 stop 底下的某个子查询里（不属于 stop 这一段）。"""
    from sqlglot import exp

    return any(isinstance(p, (exp.Select, exp.Subquery)) for p in _parents(node, stop=stop))


def _own_columns(node: Any) -> list[Any]:
    """node 里属于这一段的列（不进嵌套的子查询），不含 t.*。"""
    from sqlglot import exp

    if node is None:
        return []
    found = [node] if isinstance(node, exp.Column) else \
        [c for c in node.find_all(exp.Column) if not _nested(c, node)]
    return [c for c in found if c.name and not c.is_star]


def _is_star(item: Any) -> bool:
    """SELECT 列表里的一项是 * 或 t.*。"""
    from sqlglot import exp

    return isinstance(item, exp.Star) or (isinstance(item, exp.Column) and item.is_star)


def _inner_queries(query: Any, skip: Iterable[Any] = ()) -> list[Any]:
    """query 里直接套着的查询（不再往里找），按出现的先后：条件、SELECT 列表里的子查询，UNION 的各支，子查询里面
    那段。WITH 里的 CTE 定义不算（在用到它的地方才算），skip 里的节点整个跳过。"""
    from sqlglot import exp

    skipped = {id(n) for n in skip}
    found = []
    stack = list(reversed(list(query.iter_expressions())))
    while stack:
        node = stack.pop()
        if id(node) in skipped or isinstance(node, exp.With):
            continue
        if isinstance(node, exp.Query):
            found.append(node)
            continue
        stack.extend(reversed(list(node.iter_expressions())))
    return found


def _named_output(select: Any) -> bool:
    """这段 SELECT 选出来的列会被别处按名字引用：它（或者它所在的 UNION）是 FROM / JOIN 里的子查询、CTE 的定义。
    IN、EXISTS、标量子查询只看取值，选出来的列叫什么不影响口径。"""
    from sqlglot import exp

    node = select
    while isinstance(node.parent, (exp.Subquery, exp.SetOperation)):
        node = node.parent
    return isinstance(node.parent, (exp.From, exp.Join, exp.CTE, exp.Lateral))


def _alias(item: Any) -> str:
    """FROM / JOIN 项的别名（没有别名的实表、CTE 是它的名字）；表函数这类认不出的来源是空串。LATERAL 的别名
    挂在 Lateral 节点上，不在里面的子查询上。"""
    from sqlglot import exp

    return item.alias_or_name if isinstance(item, (exp.Table, exp.Subquery, exp.Lateral)) else ""


def _derived_body(item: Any) -> Any:
    """FROM / JOIN 项是子查询（含 LATERAL 子查询）时，里面那段查询；别的（实表、CTE、表函数）返回 None。"""
    from sqlglot import exp

    if isinstance(item, exp.Lateral):
        item = item.this
    return item.this if isinstance(item, exp.Subquery) else None


def _branches(query: Any) -> list[Any]:
    """一段查询的各支 SELECT，按出现的先后：UNION 这类拆开，外面套的括号去掉；认不出的返回空列表。"""
    from sqlglot import exp

    while isinstance(query, exp.Subquery):
        query = query.this
    if isinstance(query, exp.SetOperation):
        return _branches(query.this) + _branches(query.expression)
    return [query] if isinstance(query, exp.Select) else []


def _output_names(query: Any) -> list[str | None]:
    """派生来源选出来的各列叫什么（外面按这个名字引用它们）：按第一支 SELECT 的位置，* 为 None。CTE、子查询自己
    写了列名的（WITH t(n) AS …、(…) AS t(a, b)，递归 CTE 常这么写）按写的列名。"""
    from sqlglot import exp

    branches = _branches(query)
    if not branches:
        return []
    names = [None if _is_star(item) else item.alias_or_name for item in branches[0].selects]
    holder = query.parent
    if isinstance(holder, exp.Subquery) and isinstance(holder.parent, exp.Lateral) and not holder.alias:
        holder = holder.parent          # LATERAL 的别名（连同列名）挂在 LATERAL 上
    alias = holder.args.get("alias") if isinstance(holder, (exp.CTE, exp.Subquery, exp.Lateral)) else None
    columns = [c.name for c in alias.columns] if isinstance(alias, exp.TableAlias) else []
    for i, column in enumerate(columns[:len(names)]):
        names[i] = column
    return names


def _own_nodes(select: Any) -> Iterator[Any]:
    """一段 SELECT 自己的节点：不进 CTE、子查询（它们是别的段）。"""
    from sqlglot import exp

    stack = [select]
    while stack:
        node = stack.pop()
        yield node
        children = [c for c in node.iter_expressions()
                    if not isinstance(c, (exp.Select, exp.Subquery, exp.CTE, exp.With))]
        stack.extend(reversed(children))


def _datey(node: Any) -> bool:
    """node 里有日期函数、转成日期的 CAST，或者像日期的字符串。"""
    from sqlglot import exp

    kinds = tuple(k for k in (getattr(exp, n, None) for n in _DATE_FUNCS) if isinstance(k, type))
    for n in node.walk():
        if isinstance(n, kinds):
            return True
        if isinstance(n, exp.Anonymous) and str(n.name).lower() in _DATE_NAMES:
            return True
        if isinstance(n, exp.Cast) and n.to is not None and any(w in n.to.this.name for w in ("DATE", "TIME")):
            return True
        if isinstance(n, exp.Literal) and n.is_string and _DATE_TEXT.match(str(n.this)):
            return True
    return False


def _value_column(node: Any) -> Any:
    """派生来源一列的定义是不是「还是那一列」：光一列，或者同一列套着日期函数、CAST、MIN / MAX（按支付时间算的日期、
    每人最后一次付款的时间）；是的话返回那一列，不是返回 None。窗口函数（ROW_NUMBER() OVER (ORDER BY paid_at) 是排名，
    不是时间）、别的聚合、好几列算出来的、套着子查询的都不算。"""
    from sqlglot import exp

    n = _unparen(node)
    if isinstance(n, exp.Column):
        return n if n.name and not n.is_star else None
    if any(isinstance(x, (exp.Window, exp.Select, exp.Subquery)) or
           (isinstance(x, exp.AggFunc) and not isinstance(x, (exp.Min, exp.Max))) for x in n.walk()):
        return None
    cols = _own_columns(n)
    if len(cols) != 1:
        return None
    if _datey(n) or _bare_column(n) is not None or any(isinstance(x, (exp.Min, exp.Max)) for x in n.walk()):
        return cols[0]
    return None


def _unwrap_value(node: Any) -> Any:
    """剥掉只改写法、不改是哪个数的外壳（括号、类型转换、ROUND、COALESCE / IFNULL 给默认值）：最外层的
    ROUND(SUM(amount), 2)、COALESCE(SUM(amount), 0) 还是那个合计，只是显示上的取舍。"""
    from sqlglot import exp

    while True:
        node = _unparen(node)
        if isinstance(node, (exp.Cast, exp.TryCast, exp.Round)) and node.this is not None:
            node = node.this
        elif isinstance(node, exp.Coalesce) and node.expressions and \
                all(isinstance(_unparen(e), exp.Literal) for e in node.expressions):
            node = node.this
        else:
            return node


def _as_date(node: Any) -> Any:
    """「取日期」的几种写法统一成 CAST(… AS DATE)（比较用，给人看的照原样）：DATE(paid_at)、paid_at::date、
    TRY_CAST(paid_at AS DATE)，MySQL 的 DATE() 读成 TsOrDsToDate。各方言、各人写法不同，取的都是同一天；带时区偏移、
    格式的（DATE(paid_at, '+8 hours')）不是同一天，不动。"""
    from sqlglot import exp

    def to_date(n: Any) -> bool:
        return isinstance(n, exp.DataType) and n.this == exp.DataType.Type.DATE

    if isinstance(node, exp.Date) and node.this is not None and not node.args.get("zone") \
            and not node.args.get("expressions"):
        return exp.Cast(this=node.this, to=exp.DataType.build("DATE"))
    if isinstance(node, exp.TsOrDsToDate) and node.this is not None and not node.args.get("format"):
        return exp.Cast(this=node.this, to=exp.DataType.build("DATE"))
    if isinstance(node, exp.TryCast) and to_date(node.args.get("to")):
        return exp.Cast(this=node.this, to=exp.DataType.build("DATE"))
    return node


def _atomic(node: Any) -> bool:
    """算式里的一项不是别的加减乘除：两边对调不会改变怎么结合（a * b / c 换成 b / c * a 就不一样了）。"""
    from sqlglot import exp

    return not isinstance(_unparen(node), (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.IntDiv))


def _temporal_name(name: str) -> bool:
    """列名像日期、时间：英文按 catalog.is_date_column 的命名规则（paid_at、order_date），中文看结尾（支付日期）。"""
    return catalog.is_date_column(name, None) or bool(_CN_TIME.search(name))


#: 切词后有一个词是这些的列名「可能是时间」：数仓的业务日期键 date_key、统计月份 stat_month、分区 pt / ds、财年
#: fiscal_year、第几天 day_no……认不准是不是时间列（_temporal_name 认的是一定像日期的），只拿来少说：这样的列上的
#: 窗口不说成有效行过滤，另一边有它时也不说「没有按时间筛选」
_TIMEISH_WORDS = frozenset({"date", "dates", "time", "times", "day", "days", "dt", "ds", "ts", "timestamp", "datetime",
                            "month", "months", "mon", "year", "years", "yr", "fy", "fiscal", "week", "weeks", "wk",
                            "quarter", "qtr", "period", "hour", "hr", "minute", "ym", "ymd", "yyyymm", "yyyymmdd",
                            "yyyy", "pt", "datekey", "datekeys"})
_CN_TIMEISH = re.compile(r"日期|时间|时刻|年月|月份|年度|季度|账期|期间|统计期|周期|[年月日周]$")
#: 可能是日期的数字写法：yyyymm、yyyymmdd（20260901、202609，数仓常这么存日期键、月份）
_YMD_DIGITS = re.compile(r"^(?:19|20|21)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])?$")
#: 可能是日期的文字：年（'2026'）、季度（'2026Q3'、'2026-Q3'）、周（'2026-W36'）
_PARTIAL_DATE_TEXT = re.compile(r"^\s*(?:19|20|21)\d{2}(?:[-/]?[Qq][1-4]|-?[Ww]\d{1,2})?\s*$")


def _timeish_name(name: str) -> bool:
    """列名可能是时间（见 _TIMEISH_WORDS）：比 _temporal_name 宽。"""
    return _temporal_name(name) or any(w in _TIMEISH_WORDS for w in catalog.name_words(name)) or \
        bool(_CN_TIMEISH.search(name))


def _date_like_literal(node: Any) -> bool:
    """常量可能是日期：像日期的字符串（2026-09-01、2026-09、2026、20260901、2026Q3），yyyymm / yyyymmdd 的整数，
    LIKE 的模式去掉两头的 % / _ 之后像日期（'2026-09%'）。"""
    from sqlglot import exp

    node = _unparen(node)
    if not isinstance(node, exp.Literal):
        return False
    text = str(node.this).strip()
    if node.is_string:
        text = text.strip("%_")
        return bool(_DATE_TEXT.match(text) or _PARTIAL_DATE_TEXT.match(text) or _YMD_DIGITS.match(text))
    return bool(_YMD_DIGITS.match(text))


#: 拿列和值比的谓词（判空不算：x IS NOT NULL 是有效行过滤，不是时间窗口）
_WINDOW_PREDICATES = ("EQ", "NEQ", "GT", "GTE", "LT", "LTE", "In", "Between", "Like", "ILike", "RegexpLike",
                      "RegexpILike", "Glob", "SimilarTo")


def _window_like(node: Any) -> bool:
    """node 里有可能是时间窗口的比较：拿可能是时间的列（见 _timeish_name）、日期函数、像日期的常量（见
    _date_like_literal、_datey）去比。判空不算。只拿来少说：这样的条件不说成有效行过滤，另一边有它时也不说「没有按
    时间筛选」。"""
    from sqlglot import exp

    for n in node.walk():
        if type(n).__name__ not in _WINDOW_PREDICATES:
            continue
        if _datey(n) or any(_date_like_literal(x) for x in n.find_all(exp.Literal)) or \
                any(c.name and _timeish_name(c.name) for c in n.find_all(exp.Column)):
            return True
    return False


def _bounded_both_ways(tree: Any) -> bool:
    """语句里有对同一个对象的上下界（BETWEEN，或者 x >= 常量 和 x < 常量 都有）：可能是认不出的时间窗口（第几天
    day_no BETWEEN 20697 AND 20726）。只拿来少说。"""
    from sqlglot import exp

    if tree.find(exp.Between) is not None:
        return True
    low: set[str] = set()
    high: set[str] = set()
    for n in tree.find_all(exp.GT, exp.GTE, exp.LT, exp.LTE):
        left, right = _literal_value(n.this) is not None, _literal_value(n.expression) is not None
        if left == right:
            continue
        subject = n.expression if left else n.this
        upward = isinstance(n, (exp.GT, exp.GTE)) != left
        (low if upward else high).add(_fingerprint(subject))
    return bool(low & high)


def _join_kind(join: Any) -> str:
    side = str(join.args.get("side") or "").lower()
    kind = str(join.args.get("kind") or "").lower()
    if side in ("left", "right", "full"):
        return side
    if join.args.get("method"):
        return "natural"
    if kind in ("semi", "anti"):
        return kind
    if kind == "cross" or not (join.args.get("on") is not None or join.args.get("using")):
        return "cross"
    return "inner"


def _dedupe(items: Iterable[Any], key: Any) -> tuple[Any, ...]:
    seen: dict[Any, Any] = {}
    for item in items:
        seen.setdefault(key(item), item)
    return tuple(seen.values())


class _Sources(dict):
    """一段 SELECT 的来源 {别名的 name_key: _Source}。outer 是外层那段的来源：相关子查询里引用外层的别名（o.id）
    从这里找。"""

    outer: "_Sources | None" = None


@dataclass
class _Source:
    """一段 SELECT 的一个来源。kind：table（实表）/ derived（CTE、子查询、LATERAL 子查询）/ other（表函数、认不出的
    别名）。"""

    kind: str
    name: str                       # 给人看：表名（有库名就带上）、CTE 名、子查询（含 LATERAL）的别名
    key: str                        # 比较用：实表是 name_key，派生来源是编号（见 _Statement._number_derived）
    idents: tuple[Any, ...] = ()    # 表名各段的 Identifier：给人看的写法照原样写回去
    query: Any = None               # derived：里面那段查询
    role: str = ""                  # 同一个来源在这一段里出现了不止一次时，这一次是哪一个（见 _Statement._roles）
    alias: str = ""                 # 这一段里引用它的名字（别名，没有别名是表名、CTE 名）：给人看时分内外层用
    opaque: bool = False            # derived：同一段里别的来源透过去读的也是同一个来源，不透过去（见 _Statement._seal）

    @property
    def ref(self) -> str:
        """比较用的「哪一个来源」：key，自己关联自己时带上 role（orders#parent_id）。"""
        return f"{self.key}#{self.role}" if self.role else self.key


class _Statement:
    """一条语句的分析：CTE、实表、每段 SELECT 的来源，以及把表达式写成 key / text 的规则。"""

    def __init__(self, root: Any, read: str | None, schema: Mapping[str, Any] | None) -> None:
        from sqlglot import exp

        self.root = root
        self.read = read
        #: 各段 SELECT（原树上的节点），下标就是记在 meta 里的段号
        self.segments: list[Any] = []
        for select in root.find_all(exp.Select, bfs=False):
            select.meta[_SEGMENT] = len(self.segments)
            self.segments.append(select)
        self.ctes: dict[str, Any] = {}
        #: CTE 的定义（exp.CTE 节点），和 ctes 同一个键：编号也记在定义上，定义写进条件时靠它换掉名字
        self._cte_defs: dict[str, Any] = {}
        for cte in root.find_all(exp.CTE):
            if cte.alias:
                self.ctes.setdefault(name_key(cte.alias), cte.this)
                self._cte_defs.setdefault(name_key(cte.alias), cte)
        self.columns: dict[str, set[str]] = {}
        for name, meta in ((schema or {}).get("tables") or {}).items():
            if isinstance(meta, Mapping):
                self.columns[name_key(str(name))] = {name_key(str(c.get("name"))) for c in meta.get("columns") or []
                                                     if isinstance(c, Mapping) and c.get("name")}
        self._memo: dict[int, _Sources] = {}
        self._shared: set[str] | None = None
        #: 各派生来源的结构标识 {位置编号: 摘要}（见 _signature）、各派生来源一列的来历 {(里面那段的 id, 列名): 来历}
        #: （见 _origin）：都和编号无关，重新编号不用清
        self._signatures: dict[int, str] = {}
        self._origins: dict[tuple[int, str], str | None] = {}
        #: 派生来源各列的名字 {里面那段的 id: [列名]}、每段外面筛和里面筛一不一样 {(那段的 id, 列名): 说法}：只看
        #: 语法树，算一次记下来（透视、认列时反复要用）
        self._names_memo: dict[int, list[str | None]] = {}
        self._blocks_memo: dict[tuple[int, str], str | None] = {}
        self._number_derived()
        self._enliven(())

    # ---- 来源

    def _is_cte(self, table: Any) -> bool:
        return not table.text("db") and not table.text("catalog") and name_key(table.name) in self.ctes

    def _reach(self, extra: Iterable[str] = ()) -> set[str]:
        """从最外层那段查询出发（再加上 extra 里的 CTE）一路引用到的 CTE 名：FROM / JOIN 里的、条件和 SELECT 列表里的
        子查询里的都算，用到的 CTE 里面再引用的也算。CTE 的定义本身不算引用：没人用的 CTE 里引用的别的 CTE 也没用到。"""
        from sqlglot import exp

        live = {name for name in extra if name in self.ctes}
        stack = [self.root, *(self.ctes[name] for name in live)]
        while stack:
            for node in stack.pop().walk(prune=lambda n: isinstance(n, exp.CTE)):
                if isinstance(node, exp.Table) and isinstance(node.this, exp.Identifier) and node.name \
                        and self._is_cte(node) and (name := name_key(node.name)) not in live:
                    live.add(name)
                    stack.append(self.ctes[name])
        return live

    def _enliven(self, extra: Iterable[str]) -> None:
        """定下哪些段算数：最外层那段查询，加上从它一路引用到的 CTE（extra 里的 CTE 也当作用到了）。没
        用到的 CTE 里的条件、聚合、表不影响结果：收进来的话，盲算定义了只筛已付的 CTE 却直接查 orders，会被当成也筛了
        已付；定义了没用的 CTE 里多一个条件，又会被报成多筛了一遍。用到的实表（给人看时要不要写表名）、共有的列名跟着
        重算。"""
        from sqlglot import exp

        live = self._reach(extra)
        #: 没用到的 CTE 里的节点（id）：facets 只收别的段
        self._dead = {id(n) for name, cte in self._cte_defs.items() if name not in live for n in cte.this.walk()}
        tables: dict[str, _Source] = {}
        for table in self._alive(exp.Table):
            if isinstance(table.this, exp.Identifier) and table.name and not self._is_cte(table):
                source = self._table(table)
                tables.setdefault(source.key, source)
        self.tables = list(tables.values())
        #: 整条语句只用到一张实表：给人看的写法一律不写表名
        self.single = len(self.tables) <= 1
        self._shared = None

    def _alive(self, *types: Any) -> Iterator[Any]:
        """用到了的段里这些类型的节点（深度优先、按出现的先后），没用到的 CTE 里的不算（见 _enliven）。"""
        return (node for node in self.root.find_all(*types, bfs=False) if id(node) not in self._dead)

    @staticmethod
    def _items(select: Any) -> list[Any]:
        """一段 SELECT 的 FROM、JOIN 项，按出现的先后。"""
        first = select.args.get("from_")
        return ([first.this] if first is not None else []) + [j.this for j in select.args.get("joins") or []]

    def _number_derived(self) -> None:
        """给派生来源（引用 CTE 的表、FROM / JOIN 里的子查询和 LATERAL 子查询）编比较用的 key：@1、@2…，记在节点的
        meta 上。

        CTE 名、子查询的别名是各自起的（双盲复算的盲算更是），拿它作 key，同一个口径只因起的名字不同就被报成不同；
        拿里面那段查询的写法作 key 也不行：里面差一个条件，外层按它的列写的条件就全跟着变，同一处不同报好几遍，还会
        出现两边各说一句「只保留 total > 100 的行」的自相矛盾。所以单看一条语句时按位置编：从最外层往里一层一层地
        编，同一层按出现的先后；CTE 在第一次用到的地方编号，在哪儿用到都是这一个编号。写成 CTE 还是 FROM 里的子查询、
        CTE 定义的先后都不影响编号，里层多一段、少一段也不挪动外层的编号。这套编号只给 facets 用；比两条语句不靠
        它（见 explain_sql_diff：两边各自规范化，CTE 按定义起名）。
        """
        from sqlglot import exp

        ctes: dict[str, int] = {}
        count = 0
        #: 编了号的派生来源 [(位置编号, 起的名字, 里面那段查询, CTE 的 name_key 或 None)]，按编号排：facets 按它们
        #: 找各列的定义（见 _expressions）
        self._derived: list[tuple[int, str, Any, str | None]] = []

        def mark(node: Any, number: int) -> None:
            node.meta[_DERIVED] = f"@{number}"

        def visit(select: Any) -> list[Any]:
            """给这一段还没编号的 FROM / JOIN 项编号，返回要接着往里看的查询（子查询、第一次用到的 CTE 的定义）。"""
            nonlocal count
            inner = []
            for item in self._items(select):
                body = _derived_body(item)
                if body is not None:
                    if _DERIVED not in item.meta:
                        count += 1
                        mark(item, count)
                        self._derived.append((count, _alias(item), body, None))
                        inner.append(body)
                elif isinstance(item, exp.Table) and isinstance(item.this, exp.Identifier) and item.name \
                        and self._is_cte(item):
                    name = name_key(item.name)
                    if name not in ctes:
                        count += 1
                        ctes[name] = count
                        mark(self._cte_defs[name], count)
                        self._derived.append((count, self._cte_defs[name].alias, self.ctes[name], name))
                        inner.append(self.ctes[name])
                    mark(item, ctes[name])
            return inner

        queue: deque[Any] = deque([self.root])
        while queue:
            query = queue.popleft()
            items: list[Any] = []
            if isinstance(query, exp.Select):
                queue.extend(visit(query))
                items = self._items(query)
            queue.extend(_inner_queries(query, skip=items))
        # 没走到的（没用到的 CTE 里的、表函数参数里的）按出现的先后补上：每个派生来源都得有编号
        for select in self.root.find_all(exp.Select, bfs=False):
            visit(select)
        # 没用到的 CTE 也编上号（排在最后）：每个派生来源都得有编号
        for name, cte in self._cte_defs.items():
            if name not in ctes:
                count += 1
                ctes[name] = count
                mark(cte, count)
                self._derived.append((count, cte.alias, self.ctes[name], name))
        #: {位置编号: 里面那段查询}、{位置编号: 现在比较用的编号}、反过来 {比较用的编号: 位置编号}
        self._bodies = {number: body for number, _, body, _ in self._derived}
        self._labels = {number: f"@{number}" for number in self._bodies}
        self._numbers = {label: number for number, label in self._labels.items()}

    def column_key(self, source: _Source, column: str) -> str:
        """比较用的列名：按 name_key。"""
        return name_key(column)

    def _shape(self, node: Any, sources: dict[str, _Source]) -> str:
        """按结构写（render 的 shape 模式，比较的两边也按结构排）：配对用，和名字、编号无关。"""
        return self.render(self.canonical(node, sources, by="shape"), sources, "shape")

    def _signature(self, number: int) -> str:
        """派生来源的结构标识（shape 写法里引用它的地方写这个）：里面那段查询按 shape 写法写出来再取摘要。里面再套着
        的派生来源也写它们各自的结构标识，所以付款合计、退款合计两个 CTE 的同名列 total 在外面一层也分得开，和起的
        名字、编号无关；取摘要是免得一层套一层越写越长。递归 CTE 引用自己时写「@」。"""
        if number not in self._signatures:
            self._signatures[number] = _ANON
            text = self._shape(self._bodies[number], {})
            self._signatures[number] = "@" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
        return self._signatures[number]

    def _signature_of(self, label: str) -> str:
        """比较用的编号 → 那个派生来源的结构标识；认不出的编号写「@」。"""
        number = self._numbers.get(label)
        return self._signature(number) if number is not None else _ANON

    @staticmethod
    def _table(table: Any) -> _Source:
        from sqlglot import exp

        idents = tuple(i for i in (table.args.get("catalog"), table.args.get("db"), table.this)
                       if isinstance(i, exp.Identifier))
        return _Source("table", ".".join(i.name for i in idents), ".".join(name_key(i.name) for i in idents),
                       tuple(i.copy() for i in idents))

    def sources(self, select: Any) -> _Sources:
        """{别名的 name_key: 来源}。没有别名的实表按表名；认不出的来源按位置编号。"""
        from sqlglot import exp

        if id(select) in self._memo:
            return self._memo[id(select)]
        out = _Sources()
        aliases: dict[str, str] = {}
        for i, node in enumerate(self._items(select)):
            alias = _alias(node)
            slot = name_key(alias) if alias else f"#{i}"
            if isinstance(node, exp.Table) and isinstance(node.this, exp.Identifier) and node.name:
                if self._is_cte(node):
                    out[slot] = _Source("derived", node.name, node.meta.get(_DERIVED, slot), (node.this.copy(),),
                                        self.ctes[name_key(node.name)])
                else:
                    out[slot] = self._table(node)
            elif (body := _derived_body(node)) is not None:
                out[slot] = _Source("derived", alias or "子查询", node.meta.get(_DERIVED, slot),
                                    (exp.to_identifier(alias),) if alias else (), body)
            else:
                out[slot] = _Source("other", alias, slot)
            out[slot].alias = alias
            aliases[slot] = alias
        self._roles(select, out, aliases)
        self._memo[id(select)] = out
        parent = select.parent
        while parent is not None and not isinstance(parent, exp.Select):
            parent = parent.parent
        out.outer = self.sources(parent) if parent is not None else None
        self._seal(out)
        return out

    def _base(self, source: _Source) -> str:
        """一路透过「只读一个来源」的派生来源（through 能透过去的那一路），最后读的是哪个来源：「种类:key」。"""
        from sqlglot import exp

        for _ in range(8):
            if source.kind != "derived" or not isinstance(source.query, exp.Select):
                break
            inner = self.sources(source.query)
            if len(inner) != 1:
                break
            source = next(iter(inner.values()))
        return f"{source.kind}:{source.key}"

    def _seal(self, sources: dict[str, _Source]) -> None:
        """同一段里有几个来源透过去读的是同一个来源时，其中的派生来源都不透过去（就地标 opaque，见 through）。

        两个原样透出的 CTE 都读 orders（已付的一份、未付的一份）：透过去 p.amount、o.amount 都成了 orders.amount，外层
        用的是哪一份就分不出来了（SUM(p.amount) 对 SUM(o.amount)、p.amount > 100 对 o.amount > 100 都成了一样）；CTE 和
        它自己读的那张表关联（父单、子单）、同一个原样透出的 CTE 关联两次也一样，自己关联自己时 _roles 设的 role 透过去
        也就丢了。这时这几个派生来源的列照写它们自己的编号：用的是已付那份
        还是未付那份分得开。只有一个来源读那张表时照旧透过去：先在 CTE 里筛一遍再查和直接查
        是同一列。"""
        groups: dict[str, list[_Source]] = {}
        for source in sources.values():
            groups.setdefault(self._base(source), []).append(source)
        for found in groups.values():
            if len(found) > 1:
                for source in found:
                    if source.kind == "derived":
                        source.opaque = True

    @staticmethod
    def _roles(select: Any, sources: dict[str, _Source], aliases: Mapping[str, str]) -> None:
        """同一个来源在这一段里出现了不止一次（自己关联自己：orders o1 JOIN orders o2、base b1 JOIN base b2）时，各自
        按在关联条件（ON、WHERE 里两列相等）里出的是哪几列区分，记在 role 上（就地改）：比较用的写法写「来源#列」，
        和别名、先后都无关。b2.user_id = b1.referrer_id 里 b1 出 referrer_id、b2 出 user_id，b1.total > b2.total 写反了
        就认得出来；不然两边都写成 base.total，写反了也一样。出的列一样（b1.user_id = b2.user_id）分不出，也不用分：
        两个对调过来意思一样。给人看的写法这时写别名：都写 orders.amount 读不出是哪一个。"""
        from sqlglot import exp

        groups: dict[str, list[str]] = {}
        for slot, source in sources.items():
            if source.kind in ("table", "derived"):
                groups.setdefault(source.key, []).append(slot)
        repeated = [slots for slots in groups.values() if len(slots) > 1]
        if not repeated:
            return
        conds = [j.args["on"] for j in select.args.get("joins") or [] if j.args.get("on") is not None]
        if (where := select.args.get("where")) is not None:
            conds.append(where.this)
        joined: dict[str, set[str]] = {}
        for cond in conds:
            for eq in _conjuncts(cond):
                n = _unparen(eq)
                if not isinstance(n, exp.EQ):
                    continue
                cols = [_unparen(n.this), _unparen(n.expression)]
                slots = [name_key(c.table) if isinstance(c, exp.Column) and c.table else None for c in cols]
                if None in slots or slots[0] == slots[1] or not all(s in sources for s in slots):
                    continue
                for col, slot in zip(cols, slots):
                    joined.setdefault(slot, set()).add(name_key(col.name))
        for slots in repeated:
            roles = {slot: "+".join(sorted(joined.get(slot, ()))) for slot in slots}
            for slot in slots:
                if roles[slot] and list(roles.values()).count(roles[slot]) == 1:
                    sources[slot].role = roles[slot]
                    if aliases.get(slot):
                        sources[slot].idents = (exp.to_identifier(aliases[slot]),)

    def _exposes(self, source: _Source, column: str) -> bool:
        """这个来源可能有这一列：实表看表结构（没给表结构就当可能有），CTE、子查询看它选出来的列。"""
        from sqlglot import exp

        if source.kind == "table":
            return source.key not in self.columns or name_key(column) in self.columns[source.key]
        if source.kind == "derived" and isinstance(source.query, exp.Query):
            return any(called is None or name_key(called) == name_key(column)
                       for called in self.names(source.query))
        return True

    def locate(self, column: Any, sources: dict[str, _Source]) -> tuple[str | None, _Source | None]:
        """列 → (来源的别名键, 来源)。别名在这一段找不到的，到外层找（相关子查询引用外层），再找不到按「别的来源」
        照写（别名写错了）；没加前缀、又有不止一个来源可能有它的，返回 (None, None)。"""
        qualifier = column.table
        if qualifier:
            slot = name_key(qualifier)
            if slot in sources:
                return slot, sources[slot]
            outer = getattr(sources, "outer", None)
            while outer is not None:
                if slot in outer:
                    return None, outer[slot]        # 外层的来源：不算这一段的关联
                outer = outer.outer
            return None, _Source("other", qualifier, slot, (column.args["table"].copy(),))
        hits = [(slot, s) for slot, s in sources.items() if self._exposes(s, column.name)]
        return hits[0] if len(hits) == 1 else (None, None)

    @staticmethod
    def _depth(column: Any, sources: dict[str, _Source]) -> int:
        """列是往外第几层那段的来源的（相关子查询引用外层的别名 o.id 是 1）；这一段的、认不出的是 0。

        比较用的写法里外层的列另带「^」（orders."id^"）：相关子查询读的表和外层是同一张时（orders 里找子单、父单），
        c.parent_id = o.id 和 c.id = o.parent_id 不带标记都成了 orders.id = orders.parent_id，分不出来。标记按列所在
        那一段算，从哪一段开始写都一样。"""
        qualifier = column.table
        if not qualifier:
            return 0
        slot = name_key(qualifier)
        if slot in sources:
            return 0
        outer, depth = getattr(sources, "outer", None), 1
        while outer is not None:
            if slot in outer:
                return depth
            outer, depth = outer.outer, depth + 1
        return 0

    def _pass(self, source: _Source | None, column: str) -> tuple[_Source, str] | None:
        """派生来源只读一个来源、把它的这一列原样透出来（直接选出这一列，或者 SELECT *）时，里面一层的 (来源, 列名)；
        不是原样透出（算出来的列、读好几个来源）返回 None。先认写了名字的列：SELECT *, amount * 0.9 AS net 里的 net
        是算出来的，不是里面那张表的 net。"""
        from sqlglot import exp

        if source is None or source.kind != "derived" or not isinstance(source.query, exp.Select):
            return None
        inner = self.sources(source.query)
        if len(inner) != 1:
            return None
        only = next(iter(inner.values()))
        names = self.names(source.query)
        named = [item for item, called in zip(source.query.selects, names)
                 if called is not None and name_key(called) == name_key(column)]
        if named:
            target = _unparen(named[0].this if isinstance(named[0], exp.Alias) else named[0])
            return (only, target.name) if isinstance(target, exp.Column) else None
        return (only, column) if None in names else None

    def names(self, query: Any) -> list[str | None]:
        """派生来源选出来的各列叫什么（见 _output_names），算一次记下来。"""
        if id(query) not in self._names_memo:
            self._names_memo[id(query)] = _output_names(query)
        return self._names_memo[id(query)]

    def _blocks(self, select: Any, column: str) -> str | None:
        """见 _blocking，算一次记下来。"""
        memo = (id(select), name_key(column))
        if memo not in self._blocks_memo:
            self._blocks_memo[memo] = self._blocking(select, column)
        return self._blocks_memo[memo]

    @staticmethod
    def _blocking(select: Any, column: str) -> str | None:
        """在这一段外面按 column 筛和在它里面筛结果不同时，说它做了什么（「取前几行」「窗口函数」）；一样返回 None。

        取前几行（LIMIT、OFFSET）：先取前 10 行再筛已付，和先筛已付再取前 10 行，取到的不是同一批。窗口函数（含
        DISTINCT ON）：先算每人的第一单再筛已付（第一单就是已付的人），和先筛已付再算（有已付订单的人）不是一回事；
        只有 column 是每个窗口的分区键时一样（按分区键筛掉的是整个分区，分区里的排名、合计不受影响）。"""
        from sqlglot import exp

        if select.args.get("limit") is not None or select.args.get("offset") is not None:
            return "limit"
        distinct = select.args.get("distinct")
        if isinstance(distinct, exp.Distinct) and distinct.args.get("on") is not None:
            keys = {name_key(_unparen(e).name) for e in distinct.args["on"].expressions
                    if isinstance(_unparen(e), exp.Column)}
            if name_key(column) not in keys:
                return "window"
        for node in _own_nodes(select):
            if isinstance(node, exp.Window):
                keys = {name_key(_unparen(e).name) for e in node.args.get("partition_by") or []
                        if isinstance(_unparen(e), exp.Column)}
                if name_key(column) not in keys:
                    return "window"
        return None

    def through(self, source: _Source | None, column: str, *, pierce: bool = False,
                unseal: bool = False) -> tuple[_Source | None, str]:
        """CTE、子查询只是把一张表的列原样透出来（SELECT *，或者直接选出这一列）时，换成那张表的列：
        原流程直接查 orders、盲算先在 CTE 里筛一遍再查，算的是同一列。

        这一段取前几行、或者算了窗口函数时不透过去（见 _blocks），照写这个派生来源的列：外面的 t.status = 'paid'
        和写在 t 里面的 status = 'paid' 不是同一个条件，透过去就都成了 orders.status = 'paid'，分不出来。pierce 时
        不管这些，一直透到底：拿来认出两边是不是同一个条件、只是筛的先后不同（见 Part.pierce）。

        同一段里别的来源透过去读的也是同一个来源时（opaque，见 _seal）不透过去，pierce 也不透：透过去就分不出是
        哪一份了。unseal 时连这个也不管，只拿来给人指出最后读的是哪张表的哪一列（时间条件的 detail）。"""
        for _ in range(4):
            if source is not None and source.opaque and not unseal:
                break
            found = self._pass(source, column)
            if found is None or (not pierce and self._blocks(source.query, found[1])):
                break
            source, column = found
        return source, column

    def resolve(self, column: Any, sources: dict[str, _Source], *, pierce: bool = False,
                unseal: bool = False) -> tuple[_Source | None, str]:
        return self.through(self.locate(column, sources)[1], column.name, pierce=pierce, unseal=unseal)

    def _definition(self, source: _Source | None, column: str) -> list[tuple[Any, _Sources]] | None:
        """派生来源一列在里面的定义：[(定义的表达式, 它那一支的来源)]，UNION 每支一项（按列的位置）。只有 SELECT *
        透出来的列，定义就是里面那一支的同名列。认不出（不是派生来源、没有这一列、某一支在那个位置是 *）返回 None。"""
        from sqlglot import exp

        if source is None or source.kind != "derived" or source.query is None:
            return None
        branches = _branches(source.query)
        if not branches:
            return None
        names = [None if called is None else name_key(called) for called in self.names(source.query)]
        if name_key(column) not in names:
            if None not in names:
                return None
            return [(exp.column(column), self.sources(branch)) for branch in branches]
        position = names.index(name_key(column))
        out = []
        for branch in branches:
            if position >= len(branch.selects) or _is_star(branch.selects[position]):
                return None
            item = branch.selects[position]
            out.append((item.this if isinstance(item, exp.Alias) else item, self.sources(branch)))
        return out

    def shared(self) -> set[str]:
        """好几张表都有的列名（name_key）：这条语句里出现时挂在不止一张表上（orders.order_id、refunds.order_id），
        或者表结构里不止一张用到的表有它。给人看时只有这些列写表名，其余只写列名，读起来和原 SQL 一样短。"""
        from sqlglot import exp

        if self._shared is None:
            owners: dict[str, set[str]] = {}
            for select in self._alive(exp.Select):
                sources = self.sources(select)
                for node in _own_nodes(select):
                    if isinstance(node, exp.Column) and node.name and not node.is_star:
                        source, name = self.resolve(node, sources)
                        if source is not None:
                            owners.setdefault(name_key(name), set()).add(source.ref)
            used = [t.key for t in self.tables]
            self._shared = {name for name, keys in owners.items()
                            if len(keys) > 1 or sum(1 for t in used if name in self.columns.get(t, ())) > 1}
        return self._shared

    # ---- 写成 key / text

    @staticmethod
    def _owner(node: Any) -> Any:
        """node 往上最近的一段 SELECT（带段号的，node 本身是也算）；没有就是 None：node 在调用方那一段里。"""
        from sqlglot import exp

        while node is not None:
            if isinstance(node, exp.Select) and node.meta.get(_SEGMENT) is not None:
                return node
            node = node.parent
        return None

    def scope(self, node: Any, default: dict[str, _Source]) -> dict[str, _Source]:
        """node 所在那一段的来源。条件里套着的子查询（EXISTS、IN (SELECT …)）是另一段：别名在那一段里定义，相关
        子查询还要到外层找。复制出来的子查询带着段号，按段号找回原树上那一段算好的来源；不在子查询里就是 default。"""
        owner = self._owner(node)
        return default if owner is None else self.sources(self.segments[owner.meta[_SEGMENT]])

    def render(self, node: Any, sources: dict[str, _Source], mode: str = "text") -> str:
        """七种写法。比较用的五种（_COMPARING），名字按 name_key：key、loose 见 Part，key 写实表的表名，loose 不写，
        CTE、子查询都写编号；pierce 同 key，只是一路透过取前几行、算窗口函数的派生来源（见 through），拿来认出同一个
        条件筛的先后不同；anon 写实表的表名、派生来源一律写「@」（排比较的两边谁在前时先分实表的列和派生来源的列）；
        shape 写实表的表名、派生来源写它的结构标识（见 _signature：配对用，和名字、编号都无关）。text、full 给人看
        （见 Part），text 只在好几张表都有这一列时写真实表名，full 一律写（关联条件说的就是两张表各出哪一列，用 full）。

        node 里套着的子查询逐段改写：每一列按它所在那一段的来源认，子查询里的表去掉别名（列已经写成了真实表名）。
        子查询里拿两张表的列互相比的（相关子查询），给人看时和关联条件一样写表名；内外读的是同一个来源（orders 里找
        子单）时写表名分不出谁是谁，照写别名、留着表别名。比较用的写法里外层的列在列名后面带「^」（见 _depth），各自
        起的名字去掉（见 _drop_names）。
        """
        from sqlglot import exp

        copy = node.copy()
        if mode in _COMPARING:
            copy = copy.transform(_as_date, copy=False)
        groups: dict[int, tuple[Any, list[Any]]] = {}
        for col in copy.find_all(exp.Column):
            if col.name and not col.is_star:
                owner = self._owner(col)
                groups.setdefault(id(owner), (owner, []))[1].append(col)
        #: 给人看时照写别名的段（id）：里面的列和外层的列读的是同一个来源（见下）
        keep: set[int] = set()
        for owner, cols in groups.values():
            scope = sources if owner is None else self.sources(self.segments[owner.meta[_SEGMENT]])
            found = [(col, *self.resolve(col, scope, pierce=mode == "pierce"), self._depth(col, scope))
                     for col in cols]
            here = mode
            seen = {(s.ref, depth) for _, s, _, depth in found if s is not None}
            # 同一个来源既是这一段的、又是外层的（相关子查询读的表和外层是同一张）：写表名分不出谁是谁，写别名
            clash = {ref for ref, _ in seen if sum(1 for other, _ in seen if other == ref) > 1}
            if owner is not None and mode == "text" and len(seen) > 1:
                here = "full"
            for col, source, name, depth in found:
                alias = None
                if mode not in _COMPARING and source is not None and source.ref in clash:
                    located = self.locate(col, scope)[1]
                    alias = col.args["table"].copy() if col.table else \
                        exp.to_identifier(located.alias) if located is not None and located.alias else None
                    keep.add(id(owner))
                self._write(col, source, name, here, depth=depth, alias=alias)
        for table in copy.find_all(exp.Table):
            if isinstance(table.this, exp.Identifier) and (owner := self._owner(table)) is not None:
                if id(owner) not in keep:
                    table.set("alias", None)
                if mode in _COMPARING:
                    for arg in ("catalog", "db", "this"):
                        ident = table.args.get(arg)
                        if isinstance(ident, exp.Identifier):
                            table.set(arg, exp.to_identifier(name_key(ident.name)))
        if mode in _COMPARING:
            self._drop_names(copy, mode)
        return copy.sql(dialect=self.read, comments=False)

    def _drop_names(self, copy: Any, mode: str) -> None:
        """比较用的写法里抹掉套着的子查询里各自起的名字（就地改）：引用 CTE 的表、FROM 里的子查询（含 LATERAL）、
        子查询里自带的 CTE 定义写编号（anon 写「@」，shape 里引用 CTE 的表写它的结构标识、别的写「@」；引用它们的列
        已经照这样写了）；只看取值的子查询（IN、EXISTS、标量子查询）去掉选出来的列名。FROM 里的子查询、CTE 定义选出来
        的列名留着：外层按这个名字引用它们，去掉了就分不出外层用的是哪一列。复制出来的那段本身没有父节点，认不出是
        FROM 里的子查询，它选出来的列名也会去掉。"""
        from sqlglot import exp

        for node in copy.find_all(exp.Table, exp.Subquery, exp.Lateral, exp.CTE):
            derived = node.meta.get(_DERIVED)
            if not derived:
                continue
            if mode == "shape":
                # 引用 CTE 的表写它是哪个（结构标识）；FROM 里的子查询、CTE 定义本身整段写在这里，不用再标
                mark = exp.to_identifier(self._signature_of(derived) if isinstance(node, exp.Table) else _ANON)
            else:
                mark = exp.to_identifier(_ANON if mode == "anon" else derived)
            if isinstance(node, exp.Table):
                for arg in ("catalog", "db", "alias"):
                    node.set(arg, None)
                node.set("this", mark)
            elif isinstance(node.args.get("alias"), exp.TableAlias):
                node.args["alias"].set("this", mark)
            else:
                node.set("alias", exp.TableAlias(this=mark))
        for select in copy.find_all(exp.Select):
            if not _named_output(select):
                select.set("expressions", [e.this if isinstance(e, exp.Alias) else e for e in select.expressions])

    def _write(self, col: Any, source: _Source | None, name: str, mode: str, *, depth: int = 0,
               alias: Any = None) -> None:
        """按 render 的写法改写一列（就地改）。depth 是它属于往外第几层的来源（见 _depth）：比较用的写法里另带
        「^」。alias 是给人看时照写的别名（内外层读同一个来源时，见 render）：这时列名也照原样写，不写透过去的。"""
        from sqlglot import exp

        quoted = isinstance(col.this, exp.Identifier) and bool(col.this.args.get("quoted"))
        if mode in _COMPARING:
            if source is None:
                owner = None
            elif source.kind == "derived":
                owner = _ANON if mode == "anon" else source.ref if mode != "shape" else \
                    self._signature_of(source.key) + (f"#{source.role}" if source.role else "")
            else:
                owner = None if mode == "loose" else source.ref
            # 外层的列在列名后面带标记（不带在表名上：表名带了标记要加引号，排比较的两边时外层的列就都排到前面去了，
            # 只该在两边表名一样时才影响先后）
            column = self.column_key(source, name) if source is not None else name_key(name)
            col.set("this", exp.to_identifier(column + "^" * depth))
            col.set("table", exp.to_identifier(owner) if owner else None)
            col.set("db", None)
            col.set("catalog", None)
            return
        if alias is not None:
            col.set("table", alias)
            col.set("db", None)
            col.set("catalog", None)
            return
        col.set("this", exp.Identifier(this=name, quoted=quoted or not _PLAIN_NAME.match(name)))
        # 自己关联自己的（有 role）一律写别名：只写列名读不出是哪一个。不透过去的派生来源（opaque）不算「整条语句只用到
        # 一张实表」：两个 CTE 都读 orders，好几个都有的列（shared）得写出是哪一个的
        if source is None or (source.kind != "other" and mode != "full" and not source.role
                              and ((self.single and not source.opaque) or name_key(name) not in self.shared())):
            col.set("table", None)
            col.set("db", None)
            col.set("catalog", None)
        else:
            *heads, last = source.idents or (exp.to_identifier(source.name),)
            col.set("table", last.copy())
            col.set("db", heads[-1].copy() if heads else None)
            col.set("catalog", heads[-2].copy() if len(heads) > 1 else None)

    def canonical(self, node: Any, sources: dict[str, _Source], *, by: str = "key") -> Any:
        """比较的两边排好顺序（列在左、字面量在右；两边都有列的按 _rank 排），IN 的取值排好序；加法、乘法的两项也按
        这个规矩排（amount * 0.9 和 0.9 * amount 是同一个算法），只排两项都不是别的加减乘除的（见 _atomic）。返回新的
        树。套着的子查询里的比较按它那一段的来源排：拿外层的来源去认子查询的别名，排出来的先后就取决于别名怎么起。"""
        from sqlglot import exp

        def fix(n: Any) -> Any:
            found = _COMPARE.get(type(n).__name__)
            swap = (type(n).__name__, type(n).__name__) if isinstance(n, (exp.Add, exp.Mul)) and \
                _atomic(n.this) and _atomic(n.expression) else found
            if swap is not None and isinstance(n, exp.Binary):
                left, right = n.this, n.expression
                lc, rc = bool(_own_columns(left)), bool(_own_columns(right))
                here = self.scope(n, sources) if lc and rc else sources
                if (rc and not lc) or (lc and rc and self._rank(left, here, by) > self._rank(right, here, by)):
                    return getattr(exp, swap[1])(this=right, expression=left)
            elif isinstance(n, exp.In) and n.expressions and not n.args.get("query") \
                    and not any(_own_columns(e) for e in n.expressions):
                n.set("expressions", sorted(n.expressions, key=lambda e: e.sql(dialect=self.read)))
            return n

        return node.transform(fix)

    def _rank(self, node: Any, sources: dict[str, _Source], by: str = "key") -> tuple[str, str]:
        """比较的两边谁排前面：先按不写编号的写法（实表的列、派生来源的列分得开），分不出（两个派生来源的同名列）
        再按 by 的写法分。

        by 是 key 时按位置编号排；算结构标识时 by 是 shape，按结构标识排：位置编号决定不了先后。"""
        return self.render(node, sources, "anon"), self.render(node, sources, by)

    def part(self, node: Any, sources: dict[str, _Source], *, excludes: bool = False) -> Part:
        #: 用到的各列 {(哪一个来源, 列): ((给人看的表名, 列), 比较用的写法)}：按「哪一个来源」分，自己关联自己的两个别名
        #: 是两列（o1.amount、o2.amount）
        refs: dict[tuple[str, str], tuple[ColumnRef, str]] = {}
        origins: list[tuple[str, str]] = []
        roles: list[tuple[str, str, str]] = []
        stage = ""
        for col in _own_columns(node):
            located = self.locate(col, sources)[1]
            # 外层的列另带标记（见 _depth）：相关子查询里 c.amount 和外层的 o.amount 是两列
            mark = "^" * self._depth(col, sources)
            source, name = self.through(located, col.name)
            refs.setdefault((source.ref if source is not None else "", name_key(name) + mark),
                            ((source.name if source is not None else None, name),
                             f"{source.ref}.{self.column_key(source, name)}{mark}" if source is not None
                             else f".{name_key(name)}{mark}"))
            if source is not None and source.role and source.idents:
                if (note := (source.idents[-1].name, source.role, source.name)) not in roles:
                    roles.append(note)
            if source is None or source.kind != "derived":
                continue
            # 透视在这个派生来源上停住了，一路透到底却还能往里走：是它取前几行、算窗口函数挡住的
            if not stage and self.through(located, col.name, pierce=True)[0] is not source:
                stage = self._stage(source, name)
            if (origin := self._origin(source, name)) is not None:
                if (pair := (self.render(col, sources, "full"), origin)) not in origins:
                    origins.append(pair)
        full = self.render(node, sources, "full")
        key = self.render(node, sources, "key")
        pierce = self.render(node, sources, "pierce") if stage else ""
        if pierce == key:
            pierce = stage = ""
        columns = tuple(ref for ref, _ in refs.values())
        # 拿两张表的列互相比的（相关子查询里的 o.id = r.order_id）和关联条件一样，给人看也写表名
        across = len({table for table, _ in columns}) > 1
        return Part(key=key, text=full if across else self.render(node, sources),
                    columns=columns, excludes=self._excludes(node, sources) if excludes else None,
                    loose=self.render(node, sources, "loose"), full=full,
                    column_keys=tuple(written for _, written in refs.values()),
                    origins=tuple(origins), pierce=pierce, stage=stage, roles=tuple(roles))

    def _stage(self, source: _Source, column: str) -> str:
        """挡住了透视的派生来源做了什么（给人看，见 _blocks）：「取 t 的前 10 行」「算 t 的窗口函数」。"""
        from sqlglot import exp

        query = source.query
        if self._blocks(query, column) == "limit":
            limit = query.args.get("limit")
            count = limit.expression if isinstance(limit, exp.Limit) else limit.args.get("count") \
                if limit is not None else None
            written = count.sql(dialect=self.read, comments=False) if count is not None else ""
            return _say("取", source.name, "的前", written, "行") if written.isdigit() else \
                _say("按", "LIMIT / OFFSET", "取", source.name, "的行")
        return _say("算", source.name, "的窗口函数")

    def _origin(self, source: _Source, column: str, depth: int = 0) -> str | None:
        """派生来源一列的来历（给人看）：它在里面怎么算的，里面再用到的派生来源的列也换成各自的来历，一直换到实表的
        列，一律写表名（SUM(orders.amount)）。两边同名的派生来源其实不是同一个时（盲算的 t1 是原流程的 t2），光写
        「t1.total」两边一样，带上来历才说得清各自是付款合计还是退款合计。UNION 只写第一支；认不出定义、套得太深
        返回 None。"""
        if source.kind != "derived" or source.query is None or depth > 4:
            return None
        memo = (id(source.query), name_key(column))
        if memo not in self._origins:
            self._origins[memo] = None          # 递归 CTE 绕回自己时到这里为止
            found = self._definition(source, column)
            self._origins[memo] = self._expand(*found[0], depth + 1) if found else None
        return self._origins[memo]

    def _expand(self, node: Any, sources: dict[str, _Source], depth: int) -> str:
        """把 node 写成给人看的 full 写法，里面用到的派生来源的列换成各自的来历（见 _origin）。换进去的是一段
        写好的文字：先用占位符占住位置，整段写好之后再换回来；放在运算符里的加括号，免得优先级变了。"""
        from sqlglot import exp

        bare = _unparen(node)
        if isinstance(bare, exp.Column):
            source, name = self.resolve(bare, sources)
            inner = self._origin(source, name, depth) if source is not None else None
            return inner if inner is not None else self.render(bare, sources, "full")
        copy = node.copy()
        subs: dict[str, str] = {}
        for col in _own_columns(copy):
            source, name = self.resolve(col, sources)
            inner = self._origin(source, name, depth) if source is not None else None
            if inner is None:
                continue
            mark = exp.Placeholder(this=f"sqlsem{len(subs)}")
            wrap = " " in inner and isinstance(col.parent, (exp.Binary, exp.Unary)) and \
                not isinstance(col.parent, exp.Paren)
            subs[mark.sql(dialect=self.read)] = f"({inner})" if wrap else inner
            col.replace(mark)
        text = self.render(copy, sources, "full")
        for mark, inner in subs.items():
            text = text.replace(mark, inner)
        return text

    def _excludes(self, node: Any, sources: dict[str, _Source]) -> str | None:
        """排除写法去掉否定之后排除的是什么：a <> 1 → a = 1；NOT IN、NOT LIKE、NOT BETWEEN 同理；IS NOT NULL → 「a 为空」。
        各方言把否定放的位置不一样（NOT 包一层，或者节点上带 negate），两种都认。"""
        from sqlglot import exp

        n = _unparen(node)
        if isinstance(n, exp.NEQ):
            return self.render(exp.EQ(this=n.this.copy(), expression=n.expression.copy()), sources)
        inner = None
        if isinstance(n, exp.Not):
            inner = _unparen(n.this).copy()
        elif n.args.get("negate"):
            inner = n.copy()
            inner.set("negate", None)
        if inner is None:
            return None
        if isinstance(inner, exp.Is) and isinstance(_unparen(inner.expression), exp.Null):
            return f"{self.render(inner.this, sources)} 为空"
        return self.render(inner, sources)

    # ---- 各个方面

    def facets(self, extra: Iterable[str] = ()) -> Facets:
        """各个方面。extra 是没用到、也照样收进来的 CTE（name_key），别的没用到的 CTE 不收（见 _enliven）。
        条件、分组键、连接、聚合、HAVING 都记下在哪一层（见 _layers），按（层, key）去重。"""
        from sqlglot import exp

        self._enliven(extra)
        tags = self._layers(extra)
        joins: list[JoinFacet] = []
        where: list[Part] = []
        time: list[TimePredicate] = []
        group: list[Part] = []
        distinct: list[str] = []
        aggregates: list[Aggregate] = []
        having: list[Part] = []
        used: list[Part] = []
        for select in self._alive(exp.Select):
            sources = self.sources(select)
            # 没走到的段（条件、SELECT 列表里的子查询，以及只在它们里面用到的派生来源）在「?」那一层
            layer = tags.get(id(select), "?")
            if select is self.root or not self._in_condition(select):
                used += [Part.plain(s.ref, s.name) for s in sources.values()]
            drafts = self._join_drafts(select, sources)
            clause = select.args.get("where")
            for cond in _conjuncts(clause.this) if clause is not None else []:
                canon = self.canonical(cond, sources)
                if self._joins_on_where(canon, sources, drafts):
                    continue
                part = replace(self.part(canon, sources, excludes=True), scope=layer)
                where.append(part)
                self._rejects(canon, part, sources, drafts)
                if found := self._time(canon, sources):
                    time.append(replace(found, condition=replace(found.condition, scope=layer)))
            joins += [replace(self._join(d, sources), scope=layer) for d in drafts]
            keys = self._group(select, sources)
            if not keys and (keys := self._distinct_keys(select, sources)):
                distinct.append(layer)
            group += [replace(p, scope=layer) for p in keys]
            aggregates += [replace(g, scope=layer) for g in self._aggregates(select, sources)]
            having += [replace(p, scope=layer) for p in self._having(select, sources)]
        derived = [Part.plain(self._labels[number], name or "子查询")
                   for number, name, body, _ in self._derived if id(body) not in self._dead]
        return Facets(
            dialect=self.read,
            tables=tuple(Part.plain(s.key, s.name) for s in self.tables),
            joins=_dedupe(joins, lambda j: (j.scope, j.kind, j.table.key, j.on.key if j.on else None)),
            where=_dedupe(where, lambda p: (p.scope, p.key)),
            time=_dedupe(time, lambda t: (t.condition.scope, t.condition.key)),
            group_by=_dedupe(group, lambda p: (p.scope, p.key)),
            aggregates=_dedupe(aggregates, lambda a: (a.scope, a.func, a.arg.key, a.distinct)),
            having=_dedupe(having, lambda p: (p.scope, p.key)),
            limit=self._limit(),
            limits=tuple(self._limits()),
            sources=_dedupe(used, lambda p: p.key),
            expressions=_dedupe(self._expressions(), lambda c: (c.scope_key, c.name, c.expr.key)),
            derived=_dedupe(derived, lambda p: p.key),
            set_ops=tuple(self._set_ops(tags)),
            distinct=tuple(dict.fromkeys(distinct)),
            grain=self._grain(self.root),
            scopes=self._scope_parts(tags),
        )

    # ---- 层

    def _layers(self, extra: Iterable[str] = ()) -> dict[int, str]:
        """每段 SELECT 在哪一层：{段的 id: 层的标记}（见 Part.scope）。

        最外层是空串，UNION 的第 k 支（k ≥ 2）在后面加「|k」。FROM / JOIN 里的派生来源（CTE、子查询、LATERAL 子查询）
        是它自己的位置编号；只从一个来源里筛出一部分行的（见 _transparent）
        和用到它的那一段算同一层：写在这种 CTE 里的条件和写在外层是一回事。读同一个来源的几个派生来源同在一段时
        （见 _seal）各算各的层：两个 CTE 里各有一个 is_deleted = 0 是两个条件。派生来源在哪一层按在 FROM / JOIN 里
        第一次用到它的地方定，从外往里一层一层地走。没走到的段（条件、SELECT 列表里的子查询，以及只在它们里面用到
        的派生来源）由 facets 记成「?」。extra 是没用到、也照样收进来的 CTE，在它自己的编号那一层。"""
        from sqlglot import exp

        tags: dict[int, str] = {}
        queue: deque[tuple[Any, str]] = deque()

        def push(query: Any, tag: str) -> None:
            for i, branch in enumerate(_branches(query)):
                queue.append((branch, tag if i == 0 else f"{tag}|{i + 1}"))

        def drain() -> None:
            while queue:
                select, tag = queue.popleft()
                if id(select) in tags:
                    continue
                tags[id(select)] = tag
                sources = self.sources(select)
                for i, item in enumerate(self._items(select)):
                    body, label = _derived_body(item), item.meta.get(_DERIVED)
                    if body is None and isinstance(item, exp.Table) and isinstance(item.this, exp.Identifier) \
                            and item.name and self._is_cte(item):
                        body = self.ctes[name_key(item.name)]
                    if body is None or not label:
                        continue
                    alias = _alias(item)
                    source = sources.get(name_key(alias) if alias else f"#{i}")
                    lifted = source is not None and not source.opaque and self._transparent(item, body)
                    push(body, tag if lifted else label)

        push(self.root, "")
        drain()
        for name in extra:
            if name in self._cte_defs and (label := self._cte_defs[name].meta.get(_DERIVED)):
                push(self.ctes[name], label)
        drain()
        return tags

    def _transparent(self, item: Any, body: Any) -> bool:
        """派生来源只是从一个来源里筛出一部分行（不分组、不聚合、不去重、不是 UNION，也不是 LATERAL）：写在它里面的
        条件和写在用到它的那一段是一回事，算同一层（见 _layers）。各列怎么算不影响有哪些行；取前几行、算窗口函数的
        也算同一层，筛的先后另由 Part.pierce / stage 说。"""
        from sqlglot import exp

        if isinstance(item, exp.Lateral) or not isinstance(body, exp.Select) or len(self._items(body)) != 1:
            return False
        if any(body.args.get(k) is not None for k in ("group", "having", "distinct")):
            return False
        return not self._aggregating(body)

    @staticmethod
    def _aggregating(select: Any) -> bool:
        """这一段自己算了聚合（窗口函数、套着的子查询里的不算）。"""
        from sqlglot import exp

        return any(isinstance(n, exp.AggFunc) and not isinstance(n.parent, exp.Window) for n in _own_nodes(select))

    def _scope_parts(self, tags: Mapping[int, str]) -> tuple[Part, ...]:
        """各层给人看的说法（Part.plain(层的标记, 说法)）：最外层「外层」，派生来源写它的名字，UNION 的各支写「t 的
        UNION 第 2 支」，条件里的子查询「条件的子查询」。"""
        names = {self._labels[number]: name or "子查询" for number, name, _, _ in self._derived}
        unions = {self._labels[number] for number, _, body, _ in self._derived if len(_branches(body)) > 1}
        root_union = len(_branches(self.root)) > 1
        out: dict[str, Part] = {}
        for tag in dict.fromkeys([*tags.values(), "?"]):
            head, _, branch = tag.partition("|")
            if head == "?":
                text = "条件的子查询"
            elif head == "":
                text = f"UNION 第 {branch or 1} 支" if root_union else "外层"
            else:
                name = names.get(head, "子查询")
                text = _say(name, "的 UNION 第", branch or "1", "支") if head in unions else name
            out[tag] = Part.plain(tag, text)
        return tuple(out.values())

    def _set_ops(self, tags: Mapping[int, str]) -> list[SetOpFacet]:
        """各段的 UNION、INTERSECT、EXCEPT（见 SetOpFacet），条件里的子查询里的不算。"""
        from sqlglot import exp

        out = []
        for node in self._alive(exp.SetOperation):
            parent = node.parent
            while isinstance(parent, exp.Subquery):
                parent = parent.parent
            branches = _branches(node)
            if isinstance(parent, exp.SetOperation) or not branches:
                continue                    # 只看一串合并最上面的那一个
            tag = tags.get(id(branches[0]), "?")
            if tag != "?":
                out.append(SetOpFacet(ops=tuple(self._ops(node)), branches=len(branches),
                                      scope=self._scope_name(node), scope_key=tag))
        return out

    @classmethod
    def _ops(cls, node: Any) -> list[str]:
        """一串合并从左到右各处的写法：UNION、UNION ALL、INTERSECT、EXCEPT ALL……"""
        from sqlglot import exp

        while isinstance(node, exp.Subquery):
            node = node.this
        if not isinstance(node, exp.SetOperation):
            return []
        word = {"Union": "UNION", "Intersect": "INTERSECT", "Except": "EXCEPT"}.get(type(node).__name__,
                                                                                  type(node).__name__.upper())
        if not node.args.get("distinct"):
            word += " ALL"
        return cls._ops(node.this) + [word] + cls._ops(node.expression)

    def _distinct_keys(self, select: Any, sources: dict[str, _Source]) -> list[Part]:
        """SELECT DISTINCT（不带 ON）选出来的各项。没有 GROUP BY、没有聚合时，对这几项去重和按它们分组出的行一模一样，
        当这一段的分组键收（见 Facets.distinct）：SELECT DISTINCT user_id 和 GROUP BY user_id 不算不同，一边在子查询里
        先去重、另一边没有照报。DISTINCT ON 是每组取一行，按窗口函数算（见 _blocking）。"""
        from sqlglot import exp

        distinct = select.args.get("distinct")
        if not isinstance(distinct, exp.Distinct) or distinct.args.get("on") is not None \
                or select.args.get("group") is not None or self._aggregating(select):
            return []
        out = []
        for item in select.selects:
            if _is_star(item):
                out.append(Part.plain("*", "*"))
                continue
            target = item.this if isinstance(item, exp.Alias) else item
            out.append(self.part(self.canonical(target, sources), sources))
        return out

    def _grain(self, query: Any, depth: int = 0) -> Part | None:
        """最后出来的行按什么分：分组（GROUP BY、当分组用的 DISTINCT）是「group:各分组键」，没分组却算了聚合是
        「one」（只出一行），都没有、只从一个派生来源取数的看那个派生来源，别的是「rows」（逐行）。最外层是 UNION
        的不算（返回 None）。分组键挪了一层（先在子查询里按 user_id 分组、外层数用户数，对外层按 user_id 分组）按
        整条语句比分组键看不出来：外层出一行还是每个用户一行，看它。"""
        from sqlglot import exp

        while isinstance(query, exp.Subquery):
            query = query.this
        if not isinstance(query, exp.Select) or depth > 6:
            return None
        sources = self.sources(query)
        keys = self._group(query, sources)
        if not keys and self._aggregating(query):
            return Part.plain("one", "")
        keys = keys or self._distinct_keys(query, sources)
        if keys:
            return Part(key="group:" + ", ".join(sorted({p.key for p in keys})),
                        text="、".join(dict.fromkeys(p.text for p in keys)),
                        loose="group:" + ", ".join(sorted({p.loose for p in keys})),
                        full="、".join(dict.fromkeys(p.full for p in keys)))
        items = self._items(query)
        if len(items) == 1 and not isinstance(items[0], exp.Lateral):
            item, body = items[0], _derived_body(items[0])
            if body is None and isinstance(item, exp.Table) and isinstance(item.this, exp.Identifier) \
                    and item.name and self._is_cte(item):
                body = self.ctes[name_key(item.name)]
            if body is not None:
                return self._grain(body, depth + 1)
        return Part.plain("rows", "")

    def _rejects(self, cond: Any, part: Part, sources: dict[str, _Source], drafts: list[dict[str, Any]]) -> None:
        """同一段 WHERE 里的条件把外连接可空那一侧没关联上的行筛掉了的，记在那次连接上（见 JoinFacet.rejects）：LEFT
        JOIN 关联进来的那一侧、RIGHT JOIN 之前的各个来源可空，那一侧的列直接拿来比较、IN、LIKE、BETWEEN，或者写 IS NOT
        NULL，空值都不成立。IS NULL、OR、套在 COALESCE / IFNULL / CASE 里的不认（宁可漏报）。"""
        from sqlglot import exp

        n = _unparen(cond)
        if isinstance(n, exp.Not):
            n = _unparen(n.this)
            if isinstance(n, exp.Is):
                if not isinstance(_unparen(n.expression), exp.Null):
                    return
                n = n.this                  # IS NOT NULL
            elif type(n).__name__ not in _NULL_REJECTING:
                return
        elif type(n).__name__ not in _NULL_REJECTING:
            return
        hit = set()
        for col in _own_columns(n):
            if col is not n and any(isinstance(p, (exp.Coalesce, exp.Case, exp.If)) for p in _parents(col, stop=n)):
                continue
            if (slot := self.locate(col, sources)[0]) is not None:
                hit.add(slot)
        for draft in drafts:
            nullable = {draft["slot"]} if draft["kind"] == "left" else \
                set(draft["before_slots"]) if draft["kind"] == "right" else set()
            if draft.get("rejects") is None and hit & nullable:
                draft["rejects"] = part

    def _join_drafts(self, select: Any, sources: dict[str, _Source]) -> list[dict[str, Any]]:
        from sqlglot import exp

        first = select.args.get("from_")
        first_alias = _alias(first.this) if first is not None else ""
        drafts: list[dict[str, Any]] = []
        for i, join in enumerate(select.args.get("joins") or []):
            alias = _alias(join.this)
            slot = name_key(alias) if alias else f"#{i + 1}"
            conds: list[Any] = []
            if join.args.get("on") is not None:
                conds = [self.canonical(c, sources) for c in _conjuncts(join.args["on"])]
            elif join.args.get("using") and first_alias and alias:
                # USING (id)：按「FROM 的那张表.id = 这张表.id」写，和 ON 的写法放在一起比
                conds = [self.canonical(exp.EQ(this=exp.column(ident.name, table=first_alias),
                                               expression=exp.column(ident.name, table=alias)), sources)
                         for ident in join.args["using"]]
            before = [(key, s) for key, s in list(sources.items())[:i + 1] if key != slot]
            drafts.append({"kind": _join_kind(join), "slot": slot, "source": sources.get(slot), "conds": conds,
                           "before": [s.name for _, s in before], "before_keys": [s.ref for _, s in before],
                           "before_slots": [key for key, _ in before]})
        return drafts

    def _joins_on_where(self, cond: Any, sources: dict[str, _Source], drafts: list[dict[str, Any]]) -> bool:
        """WHERE 里两张表的列等式（逗号连接、或者 INNER JOIN 之外又写的等值关联）并进那次连接的条件，不算筛选。
        LEFT JOIN 这类外连接不并：写在 WHERE 里会把外连接变成内连接，和写在 ON 里不是一回事。"""
        from sqlglot import exp

        n = _unparen(cond)
        if not isinstance(n, exp.EQ):
            return False
        left, right = _unparen(n.this), _unparen(n.expression)
        if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
            return False
        a, b = self.locate(left, sources)[0], self.locate(right, sources)[0]
        if a is None or b is None or a == b:
            return False
        for draft in reversed(drafts):
            if draft["slot"] in (a, b) and draft["kind"] in ("inner", "cross"):
                draft["conds"].append(cond)
                draft["kind"] = "inner"
                return True
        return False

    def _join(self, draft: dict[str, Any], sources: dict[str, _Source]) -> JoinFacet:
        source = draft["source"]
        table = Part.plain("?", "?") if source is None else \
            Part(key=source.ref, text=source.name, loose=source.ref, full=source.name)
        forms = sorted((self.render(c, sources, "key"), self.render(c, sources, "loose"),
                        self.render(c, sources, "full")) for c in draft["conds"])
        on = Part(key=" AND ".join(f[0] for f in forms), text=" AND ".join(f[2] for f in forms),
                  loose=" AND ".join(sorted(f[1] for f in forms)), full=" AND ".join(f[2] for f in forms)) \
            if forms else None
        mentioned = {table for c in draft["conds"] for table, _ in self.part(c, sources).columns}
        other = [name for name in dict.fromkeys(t for t in mentioned if t) if name != table.text] if draft["conds"] \
            else draft["before"]
        # 涉及的来源按条件里直接引用的算（slot 不为 None 的是这一段的来源）：透过 CTE 找到的实表两边可能一样，
        # 关联的却是两个不同的 CTE
        sides = {source.ref} if source is not None else set()
        if draft["conds"]:
            sides |= {found.ref for c in draft["conds"] for col in _own_columns(c)
                      for slot, found in [self.locate(col, sources)] if slot is not None and found is not None}
        else:
            sides |= set(draft["before_keys"])
        # 外连接的主表、可空那一侧：LEFT JOIN 关联进来的那一侧可空，RIGHT JOIN 反过来；另一侧按关联条件里出现的来源
        # 算（没有就是它之前的各个来源），和内连接的 sides 一样不受前面别的连接先后的影响
        keep: set[str] = set()
        nullable: set[str] = set()
        if draft["kind"] in ("left", "right") and source is not None:
            near, far = {source.ref}, (sides - {source.ref}) or set(draft["before_keys"])
            keep, nullable = (far, near) if draft["kind"] == "left" else (near, far)
        return JoinFacet(kind=draft["kind"], table=table, on=on, other=tuple(sorted(other)),
                         sides=tuple(sorted(sides)), keep=tuple(sorted(keep)), nullable=tuple(sorted(nullable)),
                         rejects=draft.get("rejects"))

    def _time(self, cond: Any, sources: dict[str, _Source]) -> TimePredicate | None:
        """时间条件：一边恰好一列（可以套着日期函数、CAST），另一边没有列；列名像日期，或者用了日期函数、像日期的字符串。"""
        from sqlglot import exp

        n = _unparen(cond)
        if n.args.get("negate"):
            return None
        if isinstance(n, exp.Between):
            subject, values, op = n.this, [n.args.get("low"), n.args.get("high")], "between"
        elif isinstance(n, exp.In) and n.expressions and not n.args.get("query"):
            subject, values, op = n.this, list(n.expressions), "in"
        elif type(n).__name__ in _COMPARE and isinstance(n, exp.Binary):
            subject, values, op = n.this, [n.expression], _COMPARE[type(n).__name__][0]
        else:
            return None
        if subject is None or any(v is None or _own_columns(v) for v in values):
            return None
        column = _bare_column(subject)
        cols = [column] if column is not None else _own_columns(subject)
        if len(cols) != 1 or any(isinstance(x, (exp.AggFunc, exp.Select)) for x in subject.walk()):
            return None
        base, scope, via, datey = self._time_column(cols[0], sources)
        if not (_temporal_name(base.name) or _temporal_name(cols[0].name) or datey or _datey(subject)
                or any(_datey(v) for v in values)):
            return None
        shown = [self.render(v, sources) for v in values]
        value = f"{shown[0]} AND {shown[1]}" if op == "between" else f"({', '.join(shown)})" if op == "in" \
            else shown[0]
        # 给调用方对照数据目录的那一列一直换到实表：不透过去的派生来源（见 _seal）比较时照写它自己，可它的这一列
        # 读的还是那张表的那一列
        real, real_scope, _, _ = self._time_column(cols[0], sources, unseal=True)
        found = self.resolve(real, real_scope, pierce=True, unseal=True)
        table = found[0].name if found[0] is not None and found[0].kind == "table" else None
        return TimePredicate(column=self.part(base, scope), op=op, value=value,
                             condition=self.part(cond, sources, excludes=True), via=via,
                             table=(table, found[1]) if table else None)

    def _time_column(self, column: Any, sources: dict[str, _Source], *,
                     unseal: bool = False) -> tuple[Any, _Sources, tuple[str, ...], bool]:
        """时间条件里的那一列是 CTE、子查询算出来的、在里面是同一列套着日期函数、CAST、MIN / MAX 时（见 _value_column），
        换成里面那一列，一直换到实表：(那一列, 它那一段的来源, 路过的派生来源的列「编号.列」, 路上有没有日期函数)。
        原样透出的列 resolve 已经透过去了；这里补上算了一下的。不透过去的派生来源（见 _seal）到它为止：两个 CTE 都读
        orders 时，按已付那份的 created_at 筛还是按未付那份的筛，正是要分清的；unseal 时不管这个（见 through）。"""
        via: list[str] = []
        datey = False
        for _ in range(4):
            source, name = self.resolve(column, sources, unseal=unseal)
            if source is not None and source.opaque and not unseal:
                break
            found = self._definition(source, name) if source is not None else None
            inner = _value_column(found[0][0]) if found and len(found) == 1 else None
            if inner is None:
                break
            via.append(f"{source.key}.{self.column_key(source, name)}")
            datey = datey or _datey(found[0][0])
            column, sources = inner, found[0][1]
        return column, sources, tuple(via), datey

    @staticmethod
    def _aliases(query: Any) -> dict[str, Any]:
        """SELECT 里的别名 {name_key: 别名指的表达式}。"""
        from sqlglot import exp

        return {name_key(s.alias): s.this for s in query.selects if isinstance(s, exp.Alias) and s.alias}

    @staticmethod
    def _target(item: Any, selects: list[Any], aliases: dict[str, Any]) -> Any:
        """GROUP BY、ORDER BY 里的一项：写序号（1）的换成 SELECT 里对应的表达式，写别名的换成别名指的表达式
        （MySQL、SQLite 的读法），其余原样。"""
        from sqlglot import exp

        target = _unparen(item)
        if isinstance(target, exp.Literal) and not target.is_string and str(target.this).isdigit():
            index = int(target.this) - 1
            if 0 <= index < len(selects):
                return selects[index].this if isinstance(selects[index], exp.Alias) else selects[index]
        elif isinstance(target, exp.Column) and not target.table and name_key(target.name) in aliases:
            return aliases[name_key(target.name)]
        return target

    def _group(self, select: Any, sources: dict[str, _Source]) -> list[Part]:
        """分组键。GROUP BY 1 换成 SELECT 里对应的表达式，GROUP BY 别名换成别名指的表达式（MySQL、SQLite 的读法）。"""
        group = select.args.get("group")
        if group is None:
            return []
        selects, aliases = select.selects, self._aliases(select)
        return [self.part(self.canonical(self._target(item, selects, aliases), sources), sources)
                for item in group.expressions]

    def _having(self, select: Any, sources: dict[str, _Source]) -> list[Part]:
        """HAVING 的各个合取式。引用 SELECT 别名的列（HAVING s > 100，MySQL、SQLite 的写法）换成别名指的表达式，
        和 GROUP BY 别名一个读法：不换的话，写别名和写表达式的同一个条件会被报成两个不同的条件。"""
        from sqlglot import exp

        clause = select.args.get("having")
        if clause is None:
            return []
        aliases = self._aliases(select)
        out = []
        for cond in _conjuncts(clause.this):
            copy = cond.copy()
            for col in _own_columns(copy):
                target = aliases.get(name_key(col.name)) if not col.table else None
                # 聚合函数里的名字指的是表里的列，不是别名：SUM(amount) AS amount 之后的 SUM(amount) 不能再换
                if target is None or any(isinstance(p, exp.AggFunc) for p in _parents(col, stop=None)):
                    continue
                if col is copy:
                    copy = target.copy()
                    break
                col.replace(target.copy())
            out.append(self.part(self.canonical(copy, sources), sources))
        return out

    def _aggregates(self, select: Any, sources: dict[str, _Source]) -> list[Aggregate]:
        from sqlglot import exp

        return [self._aggregate(node, sources) for node in _own_nodes(select)
                if isinstance(node, exp.AggFunc) and not isinstance(node.parent, exp.Window)]

    def _aggregate(self, node: Any, sources: dict[str, _Source]) -> Aggregate:
        from sqlglot import exp

        try:
            func = str(node.sql_name()).lower()
        except Exception:  # noqa: BLE001 - 没有固定函数名的聚合：按类型名
            func = node.key
        arg = node.this
        distinct = isinstance(arg, exp.Distinct)
        if distinct:
            items = list(arg.expressions)
            arg = items[0] if len(items) == 1 else exp.Tuple(expressions=[i.copy() for i in items])
        if arg is None or isinstance(arg, exp.Star) or \
                (func == "count" and not distinct and isinstance(_unparen(arg), exp.Literal)):
            part = Part.plain("*", "*")         # COUNT(*)、COUNT(1)：数的是行数
        else:
            part = self.part(self.canonical(arg, sources), sources)
        return Aggregate(func=func, arg=part, distinct=distinct, text=self.render(node, sources))

    # ---- 算出来的列

    def _expressions(self) -> list[Computed]:
        """算出来的列（见 Computed）：最外层（UNION 的各支）选出来、不是光一列也不是字面量的；用到了的派生来源里
        被外面用到、又不是原样透出一列的列（原样透出的，外面的写法 resolve 已经透成了里面那一列，不用再比定义）。"""
        from sqlglot import exp

        out: list[Computed] = []
        for select in _branches(self.root):
            sources = self.sources(select)
            for item in select.selects:
                target = _unwrap_value(item.this if isinstance(item, exp.Alias) else item)
                if not self._plain_output(target):
                    out.append(self._computed([(target, sources)], None, None, "", ""))
        used = self._used()
        for number, name, body, _ in self._derived:
            label = self._labels[number]
            if id(body) in self._dead or label not in used:
                continue
            source = _Source("derived", name or "子查询", label, (), body)
            columns = [called for called in self.names(body) if called]
            columns += sorted(c for c in used[label] if c not in {name_key(x) for x in columns})
            for column in dict.fromkeys(columns):
                if name_key(column) not in used[label] or self.through(source, column)[0] is not source:
                    continue
                if found := self._definition(source, column):
                    out.append(self._computed(found, source.name, label, self.column_key(source, column), column))
        return out

    @staticmethod
    def _plain_output(target: Any) -> bool:
        """最外层选出来的一项不用按算法比：光一列（分组那一面比）、*、字面量。光一个聚合照收：另一边可能是拿同一个
        聚合再算了一下（SUM(amount) 对 SUM(amount) / 100），得配成一对说；两边只差聚合本身的，聚合那一面已经报了。"""
        from sqlglot import exp

        return isinstance(_unparen(target), (exp.Column, exp.Star, exp.Literal, exp.Null, exp.Boolean))

    def _computed(self, branches: list[tuple[Any, _Sources]], scope: str | None, scope_key: str | None, name: str,
                  shown: str) -> Computed:
        """一列的定义（UNION 每支一个）→ Computed：定义本身、聚合换成占位符之后的写法、里面的聚合。"""
        from sqlglot import exp

        exprs: list[Part] = []
        templates: list[Part] = []
        aggregates: list[Aggregate] = []
        for node, sources in branches:
            canon = self.canonical(node, sources)
            exprs.append(self.part(canon, sources))
            # 这一段自己的聚合（窗口函数、套着的子查询里的不算），按出现的先后
            own = [n for n in canon.walk(prune=lambda x: isinstance(x, (exp.Select, exp.Subquery)))
                   if isinstance(n, exp.AggFunc) and not isinstance(n.parent, exp.Window)
                   and not any(isinstance(p, exp.AggFunc) for p in _parents(n, stop=None))]
            aggregates += [self._aggregate(n, sources) for n in own]
            if own and own[0] is canon:
                templates.append(Part.plain(PLACEHOLDER, PLACEHOLDER))     # 光一个聚合
                continue
            for n in own:
                n.replace(exp.Placeholder())
            templates.append(Part(key=self.render(canon, sources, "key"), text="",
                                  loose=self.render(canon, sources, "loose")))

        def joined(parts: list[Part]) -> Part:
            if len(parts) == 1:
                return parts[0]
            return Part(key=" | ".join(p.key for p in parts), text=" | ".join(p.text for p in parts),
                        loose=" | ".join(p.loose for p in parts), full=" | ".join(p.full for p in parts),
                        columns=tuple(c for p in parts for c in p.columns),
                        column_keys=tuple(k for p in parts for k in p.column_keys),
                        origins=tuple(o for p in parts for o in p.origins))

        return Computed(scope=scope, scope_key=scope_key, name=name, shown=shown, expr=joined(exprs),
                        template=joined(templates), aggregates=tuple(aggregates))

    def _used(self) -> dict[str, set[str]]:
        """用到了的段里引用了派生来源的哪些列：{派生来源的编号: {列名的 name_key}}。原样透出的列连同里面那一层也算
        用到了；最外层的 SELECT *（t.*）用到了那个来源的每一列；USING 的列两边都算。"""
        from sqlglot import exp

        used: dict[str, set[str]] = {}

        def mark(source: _Source | None, column: str) -> None:
            for _ in range(8):
                if source is None or source.kind != "derived":
                    return
                used.setdefault(source.key, set()).add(name_key(column))
                if (found := self._pass(source, column)) is None:
                    return
                source, column = found

        def mark_all(source: _Source | None, depth: int = 0) -> None:
            if source is None or source.kind != "derived" or depth > 4:
                return
            for branch in _branches(source.query)[:1]:
                inner = self.sources(branch)
                for item, called in zip(branch.selects, self.names(source.query)):
                    if called is None:
                        for found in self._starred(item, inner):
                            mark_all(found, depth + 1)
                    else:
                        mark(source, called)

        outermost = {id(s) for s in _branches(self.root)}
        for select in self._alive(exp.Select):
            sources = self.sources(select)
            for node in _own_nodes(select):
                if isinstance(node, exp.Column) and node.name and not node.is_star:
                    mark(self.locate(node, sources)[1], node.name)
            if id(select) in outermost:
                for item in select.selects:
                    if _is_star(item):
                        for found in self._starred(item, sources):
                            mark_all(found)
            for join in select.args.get("joins") or []:
                for ident in join.args.get("using") or []:
                    for found in sources.values():
                        if self._exposes(found, ident.name):
                            mark(found, ident.name)
        return used

    @staticmethod
    def _starred(item: Any, sources: dict[str, _Source]) -> list[_Source]:
        """SELECT 列表里的 *、t.* 选的是哪几个来源。"""
        from sqlglot import exp

        if isinstance(item, exp.Column) and item.table:
            found = sources.get(name_key(item.table))
            return [found] if found is not None else []
        return list(sources.values())

    def _limit(self) -> str | None:
        from sqlglot import exp

        node = self.root.args.get("limit")
        if node is None:
            return None
        value = node.expression if isinstance(node, exp.Limit) else node.args.get("count")
        return value.sql(dialect=self.read, comments=False) if value is not None else None

    def _limits(self) -> list[LimitFacet]:
        """各段取哪几行：最外层在前，其余按出现的先后。条件里套着的子查询（WHERE、HAVING、ON 里的）不单算：
        它整个写在那个条件的 key 里，单算的话同一处不同会报两遍。"""
        from sqlglot import exp

        out = []
        for query in self._alive(exp.Select, exp.SetOperation):
            if query is not self.root and self._in_condition(query):
                continue
            if (found := self._limit_of(query)) is not None:
                out.append(found)
        return out

    def _in_condition(self, node: Any) -> bool:
        from sqlglot import exp

        while node is not None and node is not self.root:
            if isinstance(node, (exp.Where, exp.Having)) or \
                    (isinstance(node.parent, exp.Join) and node.arg_key == "on"):
                return True
            node = node.parent
        return False

    def _limit_of(self, query: Any) -> LimitFacet | None:
        """一段的 LIMIT / OFFSET，连同这一段的排序；两样都没写返回 None。"""
        from sqlglot import exp

        def written(node: Any) -> str | None:
            return node.sql(dialect=self.read, comments=False) if node is not None else None

        limit, offset = query.args.get("limit"), query.args.get("offset")
        count = written(limit.expression if isinstance(limit, exp.Limit) else limit.args.get("count")) \
            if limit is not None else None
        if offset is None and isinstance(limit, exp.Limit):
            offset = limit.args.get("offset")        # 有的方言把 OFFSET 挂在 LIMIT 底下
        skip = written(offset.expression if isinstance(offset, exp.Offset) else offset)
        if skip is not None and skip.strip() == "0":
            skip = None                              # OFFSET 0 等于没写
        if limit is None and skip is None:
            return None
        return LimitFacet(count=count, offset=skip, order=self._order(query), scope=self._scope_name(query),
                          scope_key=self._scope_key(query))

    @staticmethod
    def _scope_key(query: Any) -> str | None:
        """query 整段是哪个派生来源（CTE 的定义、FROM 里的子查询、LATERAL 子查询）的编号；最外层、UNION 的某一支、
        SELECT 列表里的子查询这类不是派生来源整段的返回 None。编号记在 CTE 定义、FROM / JOIN 项上，往外找一两层。"""
        from sqlglot import exp

        node = query
        while isinstance(node.parent, (exp.Subquery, exp.Lateral, exp.CTE)):
            node = node.parent
            if found := node.meta.get(_DERIVED):
                return found
        return None

    def _order(self, query: Any) -> tuple[tuple[Part, bool], ...]:
        """这一段的排序键 (Part, 是否降序)。ORDER BY 1、ORDER BY 别名换成 SELECT 里的表达式（UNION 按第一支的）。"""
        from sqlglot import exp

        order = query.args.get("order")
        if order is None:
            return ()
        select = query if isinstance(query, exp.Select) else None
        sources = self.sources(select) if select is not None else _Sources()
        selects = list(query.selects)
        aliases = self._aliases(query)
        out = []
        for item in order.expressions:
            target = self._target(item.this if isinstance(item, exp.Ordered) else item, selects, aliases)
            desc = bool(item.args.get("desc")) if isinstance(item, exp.Ordered) else False
            out.append((self.part(self.canonical(target, sources), sources), desc))
        return tuple(out)

    def _scope_name(self, query: Any) -> str | None:
        from sqlglot import exp

        if query is self.root:
            return None
        parent = query.parent
        if isinstance(parent, exp.Subquery) and not parent.alias and isinstance(parent.parent, exp.Lateral):
            parent = parent.parent          # LATERAL 的别名挂在 LATERAL 上，不在里面的子查询上
        if isinstance(parent, (exp.CTE, exp.Subquery, exp.Lateral)) and parent.alias:
            return parent.alias
        return "子查询"


# ==========================================================================
# 等价判定：只做保持语义的变换，比规范形式
# ==========================================================================

#: 规范形式的几级（见 _canonical_forms）。等价的结论只在同一级的两个规范形式一字不差时才下
_OPTIMIZER, _LIGHT, _PARSED = "optimizer", "light", "parsed"
#: 同一段里同名的几个来源（自己关联自己）谁先谁后逐个排列着试，取写出来最小的那个；排列数超过这个就只按原来的
#: 先后（认得出的等价改写少一些，不会错判）
_MAX_ORDERINGS = 64
#: 一条语句里一共试多少种排列（里层的子查询在外层每试一种时都要重排一遍）：用完了就都只按原来的先后
_ORDERING_BUDGET = 2000
#: 原来的名字（CTE 名、FROM 里子查询的别名）记在里面那段查询的 meta 里：起了规范的名字之后，差异说明还要照原样说
_ORIGINAL = "sql_semantics_name"
#: 写的是 LEFT / RIGHT JOIN、WHERE 把没关联上的行筛掉了、按内连接比的来源（记在那张表上，值是原来写的连接）：差异
#: 说明里照实说「写的是 LEFT JOIN，实际是 INNER JOIN」
_REJECTED = "sql_semantics_rejected"
#: 原文写的是 LEFT JOIN、因为结果只是可空一侧跳过空值的聚合而按内连接比的来源（记在那张表上，见
#: _null_blind_left_joins）：差异说明不拿它说「A 用 INNER JOIN」（原文写的是 LEFT JOIN，两种写法结果又一样）
_NULL_BLIND = "sql_semantics_null_blind"
#: 去掉了的 LEFT JOIN 关联的表（记在那一段 SELECT 上，值是表名的列表，见 _drop_unused_left_joins）：差异说明不拿
#: 「另一边没有关联这张表」说事（原文是关联了的，只是有没有这次关联结果一样）
_DROPPED_JOINS = "sql_semantics_dropped_joins"
#: 从 RIGHT JOIN 改写成的 LEFT JOIN（记在 JOIN 上）：差异说明里不说「用 LEFT JOIN」，说以哪一侧为主表
_FLIPPED = "sql_semantics_flipped"
#: GROUP BY 是从 SELECT DISTINCT 改写来的：差异说明里说「去重」
_FROM_DISTINCT = "sql_semantics_distinct"
#: 认不准是哪一列的列（记在列名的标识符上）：SQLite 里不带表名、带双引号、表结构里又没有的名字（找不到这一列时被当成
#: 字符串）；条件、标量子查询里不带表名的列（没给表结构时 qualify 归给子查询里的表，可它也可能是外层的列）。差异说明
#: 不拿它说具体的
_UNSURE = "sql_semantics_unsure"
#: 不确定的函数：每次求值结果都可能不同（两次 RANDOM() 不是同一个值），去重 AND / OR / IN 列表里一模一样的项、并子查询、
#: 合并定义一样的 CTE、改写 BETWEEN 都会改变求值的次数。有就不走规范化的两级（只比解析后的原样）
_VOLATILE = frozenset({"random", "rand", "randn", "newid", "uuid", "gen_random_uuid", "uuid_generate_v4", "random_uuid",
                       "randomblob", "setseed", "nextval", "sys_guid", "changes", "last_insert_rowid", "total_changes",
                       "sysdate", "clock_timestamp", "timeofday", "uuid_short", "random_bytes", "gen_random_bytes",
                       "pg_sleep", "sleep", "currval", "lastval", "last_insert_id", "found_rows", "row_count",
                       "get_lock", "release_lock", "benchmark", "uuid_v4", "uuidv4", "uuidv7", "gen_random_uuid_v7"})
#: 结果看行出来的先后的聚合：拼成串、拼成数组、取任意一个、取第一个 / 最后一个（类名和函数名都按小写去下划线比）
_ORDERED_AGGS = frozenset({"groupconcat", "stringagg", "arrayagg", "arrayconcatagg", "arrayuniqueagg", "jsonarrayagg",
                           "jsonobjectagg", "jsonagg", "jsonbagg", "jsongrouparray", "jsongroupobject", "listagg",
                           "xmlagg", "anyvalue", "first", "last", "firstvalue", "lastvalue", "nthvalue",
                           "jsonbobjectagg", "jsonobjectaggstrict", "arrayaggdistinct", "bitstringagg"})
#: 浮点数求和类的聚合：浮点数加法不满足结合律，加的先后不同结果的最后几位就不同（1902.2 和 1902.2000000000003）
_FLOAT_AGGS = frozenset({"sum", "avg", "stddev", "stddevpop", "stddevsamp", "variance", "variancepop", "varpop",
                         "varsamp", "covarpop", "covarsamp", "corr", "total"})
#: 拿空值做比较、算术都得空值（外连接没关联上的那一侧整行是空值）
_STRICT_UNARY = ("Neg", "Cast", "Lower", "Upper", "Length", "Abs")
_STRICT_BINARY = ("Add", "Sub", "Mul", "Div", "Mod")
#: 解析时只留下值、丢了写法的字面量：SQLite 里 0x10 是整数 16、x'10' 是 BLOB，解析出来是同一个节点；N'…'、E'…'、
#: $$…$$ 这些也一样，带了就不在语法树上判等价
_LOSSY_LITERALS = frozenset({"HEX_STRING", "BIT_STRING", "BYTE_STRING", "NATIONAL_STRING", "UNICODE_STRING",
                             "HEREDOC_STRING", "RAW_STRING"})
#: 类型名的同义写法：解析后重新生成换了个名字、在这个方言里却是同一个类型（SQLite 按名字里的字样定类型亲和：INT 和
#: INTEGER 都是整数亲和，VARCHAR 和 TEXT 都是文本亲和）。不在这里的换了名字就算解析丢了写法（SQLite 的 STRING 是数值
#: 亲和、TEXT 是文本亲和，sqlglot 解析成同一个类型）
_TYPE_SYNONYMS = {
    "sqlite": {"INT": "INTEGER", "VARCHAR": "TEXT", "CHAR": "TEXT", "NVARCHAR": "TEXT", "CLOB": "TEXT",
               "DOUBLE": "REAL", "FLOAT": "REAL"},
    "postgres": {"INTEGER": "INT", "INT4": "INT", "INT8": "BIGINT", "INT2": "SMALLINT", "FLOAT8": "DOUBLE",
                 "FLOAT4": "REAL", "BOOL": "BOOLEAN", "NUMERIC": "DECIMAL", "CHARACTER": "CHAR"},
    "mysql": {"INTEGER": "INT", "DEC": "DECIMAL", "NUMERIC": "DECIMAL", "BOOL": "BOOLEAN"},
}
#: FROM 列表到这些词就结束了（之后的逗号不再是连接）
_CLAUSE_ENDS = frozenset({"WHERE", "GROUP_BY", "HAVING", "ORDER_BY", "LIMIT", "OFFSET", "FETCH", "UNION", "INTERSECT",
                          "EXCEPT", "WINDOW", "QUALIFY", "SEMICOLON", "RETURNING", "FOR", "CONNECT_BY", "START_WITH",
                          "CLUSTER_BY", "DISTRIBUTE_BY", "SORT_BY", "PREWHERE", "LOCK"})
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_FLIP = {"GT": "LT", "LT": "GT", "GTE": "LTE", "LTE": "GTE", "EQ": "EQ", "NEQ": "NEQ"}
_NEGATE = {"EQ": "NEQ", "NEQ": "EQ", "GT": "LTE", "LTE": "GT", "GTE": "LT", "LT": "GTE"}


class _Unsupported(Exception):
    """这一级规范化处理不了这条语句的结构（递归 CTE、LATERAL、表函数……），这一级不出规范形式。"""


#: 一元加号前面可能是这些词（不是一个值的结尾）
_OPERAND_KEYWORDS = frozenset({"SELECT", "WHERE", "AND", "OR", "NOT", "ON", "WHEN", "THEN", "ELSE", "HAVING", "BY", "IN",
                               "IS", "LIKE", "GLOB", "BETWEEN", "CASE", "AS", "DISTINCT", "ALL", "SET", "VALUES",
                               "LIMIT", "OFFSET", "RETURN", "ESCAPE", "FILTER", "OVER", "PARTITION", "USING", "IF",
                               "EXISTS", "ANY", "SOME", "FROM", "JOIN", "WITH", "ORDER", "GROUP"})


#: 函数名的同义写法（原文写的名字 → 规范的名字，空串是规范形式里不是函数调用）：在这个方言里意思确实一样，sqlglot
#: 也解析成同一个节点。别的写法解析成同一个节点、意思却不同的（SQLite 的 MOD(12.5, 2) 是 0.5，12.5 % 2 先转整数得 0；
#: PostgreSQL 的 LOG10(x) 是浮点数、LOG(10, x) 是 numeric；SQLite 没有 CHAR_LENGTH，sqlglot 读成 LENGTH），两边写的
#: 函数名不一样就不判等价（见 _call_signature）。每一项都对着方言的文档核过：
#: - SQLite：IFNULL(a, b) 就是两个参数的 COALESCE；SUBSTR 和 SUBSTRING、POW 和 POWER、CEILING 和 CEIL 是同一个函数；
#:   IIF 和 CASE 生成同样的字节码；like(x, y) 就是 y LIKE x、glob(x, y) 就是 y GLOB x（SQLite 文档原话）。
#: - PostgreSQL：substr 和 substring、char_length 和 length（按字符）、now() 和 current_timestamp、pow 和 power、
#:   ceiling 和 ceil、mod(a, b) 和 a % b、variance 和 var_samp、btrim(x) 和 trim(x) 都是同一个。
#: - MySQL：IFNULL 和两个参数的 COALESCE、SUBSTR 和 SUBSTRING、INSTR(s, t) 和 LOCATE(t, s)、POSITION(t IN s) 和
#:   LOCATE(t, s)、POW 和 POWER、CEILING 和 CEIL、MOD(a, b) 和 a % b、LCASE / UCASE 和 LOWER / UPPER、一个参数的 LOG
#:   和 LN、ISNULL(x) 和 x IS NULL、IF 和 CASE 都是同一个。
_SAFE_RESPELLINGS: dict[str, dict[str, str]] = {
    "sqlite": {"IFNULL": "COALESCE", "SUBSTR": "SUBSTRING", "POW": "POWER", "CEILING": "CEIL", "IIF": "", "LIKE": "",
               "GLOB": ""},
    "postgres": {"SUBSTR": "SUBSTRING", "CHAR_LENGTH": "LENGTH", "CHARACTER_LENGTH": "LENGTH", "NOW": "",
                 "POW": "POWER", "CEILING": "CEIL", "MOD": "", "VARIANCE": "VAR_SAMP", "BTRIM": "TRIM"},
    "mysql": {"IFNULL": "COALESCE", "SUBSTR": "SUBSTRING", "INSTR": "LOCATE", "POSITION": "LOCATE", "POW": "POWER",
              "CEILING": "CEIL", "MOD": "", "LCASE": "LOWER", "UCASE": "UPPER", "LOG": "LN", "ISNULL": "", "IF": ""},
}
#: 后面跟着括号却不是函数名的词
_NOT_CALLS = frozenset({"IN", "EXISTS", "OVER", "VALUES", "AS", "ON", "USING", "FILTER", "WITHIN", "ANY", "ALL", "SOME",
                        "ROW", "AND", "OR", "NOT", "WHERE", "SELECT", "FROM", "JOIN", "HAVING", "BY", "WHEN", "THEN",
                        "ELSE", "CASE", "UNION", "INTERSECT", "EXCEPT", "WITH", "RECURSIVE", "LATERAL", "TABLE",
                        "INTO", "SET", "DISTINCT", "LIMIT", "OFFSET", "FETCH", "ORDER", "GROUP", "PARTITION", "RANGE",
                        "ROWS", "BETWEEN", "IS", "ESCAPE", "INTERVAL", "RETURN", "NULLS", "OUTER", "INNER", "CROSS",
                        "NATURAL", "FULL", "MATERIALIZED", "QUALIFY", "WINDOW", "TUPLE"})


def _mysql_dashes(text: str) -> str:
    """MySQL 里 -- 后面要跟空白（或控制字符）才是注释，不然就是两个减号：credit--1 是 credit - (-1)（MySQL 文档的
    原例）。sqlglot 一律当成注释、把后面整行吃掉，这里先把这样的 -- 拆成 「- -」 再交给它解析，意思不变。字符串
    （'…'、"…"，反斜杠和成对的引号转义）、反引号里的名字、/* … */ 和 # 注释、真正的 -- 注释里面的不动。"""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in "'\"`":
            j = i + 1
            while j < n:
                if text[j] == "\\" and ch != "`":
                    j += 2
                    continue
                if text[j] == ch:
                    if j + 1 < n and text[j + 1] == ch:
                        j += 2
                        continue
                    break
                j += 1
            out.append(text[i:j + 1])
            i = j + 1
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(text[i:j])
            i = j
            continue
        if ch == "#" or (text.startswith("--", i) and (i + 2 >= n or text[i + 2].isspace() or ord(text[i + 2]) < 32)):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(text[i:j])
            i = j
            continue
        if text.startswith("--", i):
            out.append("- ")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _executable_comment(tokens: list[Any]) -> bool:
    """MySQL / MariaDB 会执行 /*! … */、/*M! … */ 里的代码（版本号写在 ! 后面的，服务器版本够了才执行），sqlglot 当成
    普通注释扔掉。别的库里这只是注释，一样不在语法树上判等价（只会少认几对等价）。"""
    return any(c.lstrip().startswith(("!", "M!")) for t in tokens for c in (t.comments or []))


def _call_names(tokens: list[Any]) -> list[str]:
    """原文里写成「名字(」的函数名（大写）。IN (、EXISTS (、OVER ( 这类后面跟括号的关键字不算。"""
    out = []
    for token, nxt in zip(tokens, tokens[1:]):
        if nxt.token_type.name == "L_PAREN" and token.token_type.name not in ("STRING", "NUMBER", "IDENTIFIER") \
                and _PLAIN_NAME.match(token.text or "") and token.text.upper() not in _NOT_CALLS:
            out.append(token.text.upper())
    return out


#: 原文写的函数名（按同义写法换算之后）和 sqlglot 重新生成的不一样时，记在函数节点的 meta 里，比等价时算进去（见
#: _mark_surfaces、_fingerprint）
_SURFACE = "sql_semantics_surface"


def _mark_surfaces(tree: Any, text: str, read: str | None) -> bool:
    """函数节点上记下原文写的名字（换算成规范的同义写法，见 _SAFE_RESPELLINGS）——只在它和 sqlglot 重新生成的名字
    不一样时记：sqlglot 把方言里意思不同的写法解析成同一个节点时（SQLite 的 MOD(a, 2) 和 a % 2 都是 Mod；PostgreSQL 的
    LOG10(x) 和 LOG(10, x) 都是 Log），两边的语法树就因为这一项而不同，不会判等价。解析器在函数节点的 meta 里留着
    函数名在原文里的位置（start / end），按位置取原文。

    返回能不能记全：原文写的某个函数名在重新生成的写法里不见了（又不是同义写法），却没有哪个节点记下了它（解析器
    换了写法、没留位置，PostgreSQL 的 DATE_PART(…) 解析成 EXTRACT 就是这样）时返回 False，调用方就不在语法树上判等价。"""
    from sqlglot import exp

    mapping = _SAFE_RESPELLINGS.get(read or "", {})
    attached: set[str] = set()
    leaves = (exp.Column, exp.Identifier, exp.Literal, exp.Star, exp.Null, exp.Boolean, exp.Var, exp.Placeholder,
              exp.Parameter, exp.DataType)
    for node in tree.walk():
        if isinstance(node, leaves):
            continue
        start, end = node.meta.get("start"), node.meta.get("end")
        if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start <= end < len(text):
            continue
        written = text[start:end + 1].upper()
        # 只认写成「名字(」的函数调用（Mod 这类运算节点从 MOD(…) 解析出来时也带着名字的位置）
        if not _PLAIN_NAME.match(written) or not text[end + 1:].lstrip().startswith("("):
            continue
        written = mapping.get(written, written)
        try:
            generated = node.sql(dialect=read, comments=False)
        except Exception:  # noqa: BLE001 - 生成不了：照写的记
            generated = ""
        found = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(", generated)
        if written and (found is None or found.group(1).upper() != written):
            node.meta[_SURFACE] = written
            attached.add(text[start:end + 1].upper())
    try:
        tokens = _tokens(text, read)
        present = {t.text.upper() for t in _tokens(tree.sql(dialect=read, comments=False), read)}
    except Exception:  # noqa: BLE001 - 切词、重新生成不了：当成记不全
        return False
    # CAST 一类（解析成 Cast 节点、不留位置）换写法也是同一个意思（CAST(x AS INT) 和 x::INT、MySQL 的 CONVERT(x, CHAR)），
    # 也不会和别的函数撞成同一个节点（SQLite 的 CAST(x AS DATE) 重新生成成 DATE(x)，可 DATE(x) 解析出来是 Date 节点）
    return all(name in present or name in mapping or name in attached or name in ("CAST", "TRY_CAST", "CONVERT")
               for name in _call_names(tokens))


def _respelled(text: str, read: str | None, tree: Any) -> bool:
    """解析后重新生成时，原文写的函数名不见了、又不是这个方言里的同义写法（normalize_sql 用：重新生成的文本会把
    MOD() 和 % 写成一样，这样的语句改按原文逐词规范化）。"""
    try:
        tokens = _tokens(text, read)
        again = _tokens(tree.sql(dialect=read, comments=False), read)
    except Exception:  # noqa: BLE001 - 切词、重新生成不了：当成丢了写法
        return True
    present = {t.text.upper() for t in again}
    safe = _SAFE_RESPELLINGS.get(read or "", {})
    return any(name not in present and name not in safe for name in _call_names(tokens))


def _type_words(tokens: list[Any], read: str | None) -> list[str]:
    from sqlglot.parser import Parser

    synonyms = _TYPE_SYNONYMS.get(read or "", {})
    words = [t.text.upper() for t in tokens if t.token_type in Parser.TYPE_TOKENS]
    return sorted(synonyms.get(w, w) for w in words)


#: 比较运算：同一层括号、同一段条件里有两个（或者和下面的谓词各一个）没加括号地连着写，各方言的结合顺序和 sqlglot 的
#: 不一样（sqlglot 让 IS、IN、LIKE、BETWEEN 都比 = 结合得紧，还让 < > 比 = 紧）
_MISREAD_COMPARISONS = frozenset({"EQ", "NEQ", "LT", "GT", "LTE", "GTE", "NULLSAFE_EQ"})
#: 谓词：IS（含 IS NOT、IS [NOT] DISTINCT FROM）、IN、LIKE 一类、BETWEEN、PostgreSQL / SQLite 的后缀 ISNULL、NOTNULL
_MISREAD_PREDICATES = frozenset({"IS", "IN", "LIKE", "ILIKE", "GLOB", "RLIKE", "IRLIKE", "SIMILAR_TO", "BETWEEN",
                                 "ISNULL", "NOTNULL", "MATCH", "MEMBER_OF", "SOUNDS_LIKE"})
#: 按位运算和 sqlglot 当成同一级的「别的运算符」（PostgreSQL 的 #、JSON 的 -> / ->>、数组的 @> / <@ / &&……）：和别的
#: 二元运算混用时，各方言的结合顺序和 sqlglot 的不一样（MySQL 里 & 比 | 紧、<< 比 & 紧、^ 比 * 紧；PostgreSQL 里前缀 ~
#: 比 + 松；sqlglot 把它们都放在同一级、从左往右）
_MISREAD_BITWISE = frozenset({"AMP", "PIPE", "CARET", "TILDE", "HASH", "ARROW", "DARROW", "HASH_ARROW", "DHASH_ARROW",
                              "LR_ARROW", "AT_GT", "LT_AT", "DQMARK", "CARET_AT", "QMARK_AMP", "QMARK_PIPE", "HASH_DASH",
                              "PIPE_SLASH", "DPIPE_SLASH", "OPERATOR"})
#: 算术和拼接
_MISREAD_ARITHMETIC = frozenset({"PLUS", "DASH", "STAR", "SLASH", "MOD", "DIV", "DPIPE"})
#: 到这些词一段条件（表达式）就结束了：之后是另一个子句、另一项，结合顺序和前面无关。只列确定是子句、分隔的词：漏列
#: 一个只会让一段拉长、多拦下几条（宁可认不出），不会漏拦。ALL / ANY、DISTINCT、OVER、FILTER、WITH 不在里面（x > ALL
#: (…)、IS DISTINCT FROM、窗口函数、WITH TIME ZONE 都在一个表达式中间）；LEFT / RIGHT 后面紧跟括号的是函数
_MISREAD_SEGMENT_ENDS = frozenset({
    "AND", "OR", "XOR", "COMMA", "SEMICOLON", "SELECT", "FROM", "WHERE", "HAVING", "ON", "USING", "WHEN", "THEN", "ELSE",
    "GROUP_BY", "ORDER_BY", "PARTITION_BY", "LIMIT", "OFFSET", "FETCH", "UNION", "INTERSECT", "EXCEPT", "JOIN", "INNER",
    "OUTER", "LEFT", "RIGHT", "FULL", "CROSS", "NATURAL", "STRAIGHT_JOIN", "ALIAS", "ASC", "DESC", "WINDOW", "QUALIFY",
    "RETURNING", "LATERAL", "VALUES", "SET", "INTO", "FOR", "LOCK", "PREWHERE", "CLUSTER_BY", "DISTRIBUTE_BY", "SORT_BY",
    "CONNECT_BY", "START_WITH", "ROWS", "RANGE"})
#: 一个值的最后一个词（之后跟着的是中缀运算符）：用来分清 ~ 是前缀的按位取反还是中缀的正则匹配、LIKE( / ISNULL( 是
#: 函数还是谓词
_OPERAND_ENDS = frozenset({"VAR", "IDENTIFIER", "NUMBER", "R_PAREN", "R_BRACKET", "NULL", "TRUE", "FALSE", "STAR",
                           "PLACEHOLDER", "PARAMETER", "END"})
#: 字符串里的反斜杠是转义的方言（MySQL / MariaDB，以及不知道是哪种库时）：sqlglot 不认 \Z（Ctrl+Z）、\0（NUL），
#: 原样留着反斜杠，和写成 \\Z 的撞成同一个值。SQLite、PostgreSQL 的普通字符串里反斜杠就是反斜杠
_PLAIN_BACKSLASH = frozenset({"sqlite", "postgres"})
#: sqlglot 读注释的方式和库一样的方言：块注释 SQLite、MySQL 不嵌套（sqlglot 的这两种读法也不嵌套），PostgreSQL 嵌套
#: （sqlglot 也嵌套）。别的方言、不知道是哪种库时，块注释里再写 /* 就说不准注释到哪儿为止
_COMMENTS_AS_SQLGLOT = frozenset({"sqlite", "mysql", "postgres"})


def _symbolic(word: str) -> bool:
    """运算符写成符号（~、~*、@@、-|-），不是写成关键字（LIKE、ILIKE、REGEXP）。"""
    return bool(word) and not any(ch.isalnum() or ch == "_" for ch in word)


def _range_tokens(read: str | None) -> frozenset[str]:
    """这个方言里 sqlglot 当成 LIKE、IN 那一级（比 ||、+ 松）来读的词的类型名（RANGE_PARSERS）。"""
    from sqlglot.dialects.dialect import Dialect

    try:
        return frozenset(t.name for t in Dialect.get_or_raise(read).parser_class.RANGE_PARSERS)
    except Exception:  # noqa: BLE001 - 取不到：只靠下面列出来的几类
        return frozenset()


def _misread(tokens: list[Any], text: str, read: str | None) -> str | None:
    """原文里有已知会被 sqlglot 读错的写法时返回是哪一种（不在语法树上判等价，也不说具体的不同）；没有返回 None。
    按方言切出来的词逐个看、按括号深度分层（CASE … END 也算一层），不拿正则在原文上硬匹配：

    - 字符串里有反斜杠（方言是 MySQL / MariaDB，或者不知道是哪种库时，见 _PLAIN_BACKSLASH）；
    - 两个字符串常量挨着写：MySQL 里 'a' 'b' 是 'ab'，SQLite 里却是 'a' AS b，sqlglot 一律读成拼接；
    - 数字后面紧跟着字母、下划线、数字（1_000、0o17）：SQLite 3.46 起、PostgreSQL 16 起 1_000 是一千、0o17 是 15，
      sqlglot 切成数字 1 加别名 _000；
    - XOR（MySQL 里 AND 比 XOR 结合得紧，sqlglot 反过来）；方言不知道时的 ||（MySQL 里是 OR）、MySQL 以外的 &&；
    - 同一层括号、同一段条件里，比较运算和 IS / IN / LIKE / BETWEEN / 另一个比较运算没加括号地混用（a = b IS NULL、
      a < 100 IN (0, 1)、a = 100 < 200）：SQLite 里 IS、IN 和 = 同级从左往右，PostgreSQL 里 IS 比 = 松，MySQL 里
      比较运算都同级从左往右，sqlglot 让 IS / IN 先结合；
    - 同一层括号、同一段里，按位运算（& | ^ << >> ~ 和 PostgreSQL 的 # 这类）、COLLATE 和别的二元运算（算术、拼接、
      另一种按位运算）混用；
    - 写成符号、sqlglot 却当成 LIKE / IN 那一级来读的运算符（PostgreSQL 的 ~、~*、~~、~~*、@@、-|-、&<……）和别的二元
      运算混用：PostgreSQL 里它们是「别的运算符」，和 ||、&、# 同级、从左往右结合，status ~ 'p' || 'x' 是
      (status ~ 'p') || 'x'（得到 'truex' 这样的文字），sqlglot 读成 status ~ ('p' || 'x')。写成关键字的 LIKE、ILIKE
      本来就比 || 松，PostgreSQL 和 sqlglot 读得一样，不算；
    - PostgreSQL（以及不知道是哪种库时）的前缀 @（取绝对值，@ amount - 20 是 @ (amount - 20)，sqlglot 读成参数
      $amount）、|/ 和 ||/（双精度的开方，sqlglot 写成 SQRT / CBRT，numeric 的 SQRT 结果多出几位小数）；COLLATE 后面
      同一段里又有 ::（PostgreSQL 里 x COLLATE "C"::text 的 :: 套在整个 COLLATE 上，sqlglot 读成套在排序规则的名字上）；
    - 不知道是哪种库时：块注释里再写 /*（PostgreSQL 嵌套、SQLite / MySQL 不嵌套，见 _COMMENTS_AS_SQLGLOT）、后面紧跟着
      字的 --（MySQL 里 amount--1 是 amount - (-1)，别的库是注释）、#（MySQL 里是注释，PostgreSQL 里是按位异或）、双引号
      括起来的名字（MySQL 默认的 sql_mode 里 "status" 是字符串，别的库和 sqlglot 的通用读法里是列名）；
    - 前缀的 NOT 后面同一段里既有谓词、又有算术 / 拼接 / 按位运算（NOT status IN (…) * amount、NOT paid_at IS NULL * amount）：
      SQLite 里 NOT 最松，sqlglot 的 NOT 只取等值那一级。宁可多拦（NOT a = 1 + 2 其实读得对），只给兜底不会说错。

    一段条件到 AND、OR、逗号、子句关键字为止（BETWEEN 自己的 AND 不算；IS [NOT] DISTINCT FROM 的 FROM 不算）。"""
    if read not in _PLAIN_BACKSLASH and any(
            t.token_type.name.endswith("STRING") and "\\" in text[t.start:t.end + 1] for t in tokens):
        return "字符串里的反斜杠转义"
    if read not in _COMMENTS_AS_SQLGLOT and any("/*" in c for t in tokens for c in (t.comments or [])):
        return "块注释里套着 /*"
    if read is None and (_mysql_dashes(text) != text or any(t.token_type.name == "HASH" for t in tokens)):
        return "-- 和 # 在这个方言里是不是注释说不准"
    if read is None and any(t.token_type.name == "IDENTIFIER" and text[t.start:t.start + 1] == '"' for t in tokens):
        # sqlglot 的通用读法（和 PostgreSQL、SQLite 一样）把 "status" 读成名字，MySQL 默认的 sql_mode 里它是字符串：
        # WHERE "status" = 'paid' 恒为假，和 WHERE status = 'paid' 读成了同一棵树
        return "双引号括起来的是名字还是字符串说不准"
    ranged = _range_tokens(read)
    for prev, token in zip(tokens, tokens[1:]):
        if prev.token_type.name.endswith("STRING") and token.token_type.name.endswith("STRING"):
            return "两个字符串常量挨着写"
        if prev.token_type.name == "NUMBER" and token.start == prev.end + 1 and \
                (text[token.start:token.start + 1].isalnum() or text[token.start:token.start + 1] == "_"):
            return "数字的写法（1_000、0o17）"
    frames: list[dict[str, Any]] = [{}]

    def fresh(frame: dict[str, Any]) -> None:
        frame.update(compare=0, between=0, bitwise=set(), arithmetic=False, collated=False, negated=False)

    def done(frame: dict[str, Any]) -> str | None:
        if frame["compare"] >= 2:
            return "比较运算和 IS / IN / LIKE / BETWEEN 没加括号地混用"
        if frame["negated"] and frame["compare"] and (frame["arithmetic"] or frame["bitwise"]):
            # 前缀的 NOT 后面同一段里既有谓词、又有算术 / 拼接 / 按位运算（NOT status IN (…) * amount）：SQLite 里 NOT 最松、
            # * 比 IN 紧，是 NOT ((status IN (…)) * amount)；sqlglot 的 NOT 只取等值那一级，读成 (NOT status IN (…)) * amount
            return "前缀的 NOT 和谓词、算术运算没加括号地混用"
        if frame["bitwise"] and (len(frame["bitwise"]) >= 2 or frame["arithmetic"]):
            return "按位运算、COLLATE 和别的运算没加括号地混用"
        return None

    fresh(frames[0])
    i, n = 0, len(tokens)
    while i < n:
        token = tokens[i]
        kind, word = token.token_type.name, token.text.upper()
        frame = frames[-1]
        prev = tokens[i - 1] if i else None
        nxt = tokens[i + 1] if i + 1 < n else None
        after_operand = prev is not None and (prev.token_type.name in _OPERAND_ENDS or
                                              prev.token_type.name.endswith("STRING"))
        called = nxt is not None and nxt.token_type.name == "L_PAREN" and not after_operand
        if kind == "XOR" or (kind == "VAR" and word == "XOR"):
            return "XOR"
        if (kind == "DPIPE" and read is None) or (kind == "DAMP" and read != "mysql"):
            return f"{token.text} 在这个方言里的意思说不准"
        if kind == "PARAMETER" and token.text == "@" and read in ("postgres", None):
            # PostgreSQL 里前缀的 @ 是取绝对值（@ amount - 20 是 @ (amount - 20)），sqlglot 读成参数 $amount
            return "PostgreSQL 的前缀 @"
        if kind in ("PIPE_SLASH", "DPIPE_SLASH"):
            # PostgreSQL 的 |/、||/ 是双精度的开方，sqlglot 写成 SQRT / CBRT：numeric 的 SQRT 多出几位小数
            return f"PostgreSQL 的 {token.text}"
        if kind == "DCOLON" and frame["collated"]:
            # x COLLATE "C"::text：PostgreSQL 里 :: 套在整个 COLLATE 上，sqlglot 读成套在排序规则的名字上
            return "COLLATE 后面跟着 ::"
        if kind in ("L_PAREN", "L_BRACKET", "CASE"):
            frames.append({})
            fresh(frames[-1])
        elif kind in ("R_PAREN", "R_BRACKET", "END"):
            why = done(frame)
            if why:
                return why
            if len(frames) > 1:
                frames.pop()
        elif kind == "AND" and frame["between"]:
            frame["between"] -= 1
        elif (kind in _MISREAD_SEGMENT_ENDS and not (kind in ("LEFT", "RIGHT") and called)) or \
                (kind in ("DAMP", "DPIPE") and read == "mysql"):
            why = done(frame)
            if why:
                return why
            fresh(frame)
        elif kind in _MISREAD_COMPARISONS:
            # << 和 >> 切出来是两个挨着的 < / >：是移位，不是两个比较
            if kind in ("LT", "GT") and nxt is not None and nxt.start == token.end + 1 and nxt.token_type.name == kind:
                frame["bitwise"].add(token.text * 2)
                i += 2
                continue
            frame["compare"] += 1
        elif token.text == "~" and not after_operand:
            # 前缀的 ~ 是按位取反（PostgreSQL 切成 RLIKE，别的方言切成 TILDE）
            frame["bitwise"].add("~")
        elif kind in _MISREAD_PREDICATES and not (called and kind not in ("IN", "BETWEEN", "IS")):
            # MySQL 的 ISNULL(x)、SQLite 的 like(x, y)、MATCH (…) AGAINST 这类写在值的位置、后面跟括号的是函数
            frame["compare"] += 1
            if _symbolic(token.text):
                # 写成符号的 ~、~*、~~、~~*：PostgreSQL 里和 ||、& 同级，sqlglot 当成 LIKE 那一级（比 || 松）
                frame["bitwise"].add(kind)
            if kind == "BETWEEN":
                frame["between"] += 1
            elif kind == "IS":
                # IS NOT DISTINCT FROM：后面的 NOT、DISTINCT、FROM 都是这一个谓词的一部分
                j = i + 1
                if j < n and tokens[j].token_type.name == "NOT":
                    j += 1
                if j + 1 < n and tokens[j].token_type.name == "DISTINCT" and tokens[j + 1].token_type.name == "FROM":
                    i = j + 2
                    continue
                i = j
                continue
        elif kind == "NOT" and not after_operand and not (nxt is not None and nxt.token_type.name == "NULL"):
            # 前缀的 NOT（前面不是操作数）：x NOT IN / NOT LIKE / NOT BETWEEN 跟在操作数后面，不算；IS NOT 在 IS 那一支整个算过
            frame["negated"] = True
        elif kind == "NOT" and nxt is not None and nxt.token_type.name == "NULL":
            # SQLite 的后缀 x NOT NULL（前面是 IS 的已经整个算过了）
            frame["compare"] += 1
        elif kind in _MISREAD_BITWISE or (kind in ranged and _symbolic(token.text)):
            # 后一种：@@、-|-、&<、@? 这些写成符号的运算符，sqlglot 当成 LIKE 那一级，PostgreSQL 里和 ||、& 同级
            frame["bitwise"].add(kind)
        elif kind == "COLLATE":
            # MySQL 的 COLLATE 比 + 结合得紧，sqlglot 的 MySQL 读法让 a + b COLLATE x 整个套上 COLLATE：和算术、拼接、
            # 按位运算混用时当成按位运算一样拦下
            frame["bitwise"].add(kind)
            frame["collated"] = True
        elif kind in _MISREAD_ARITHMETIC:
            frame["arithmetic"] = True
        i += 1
    for frame in frames:
        why = done(frame)
        if why:
            return why
    return None


def _lost_in_parsing(text: str, read: str | None, tree: Any) -> bool:
    """原文里有 sqlglot 解析时丢掉、或者读错了、语义却不一样的写法，有就不在语法树上判等价：

    - 一元加号：SQLite 里 +a 去掉了列的类型亲和，+a = '10' 和 a = '10' 结果不同，解析出来却是同一棵树；
    - 只留下值、丢了写法的字面量（0x10 和 x'10'，见 _LOSSY_LITERALS）；
    - 会执行的注释（MySQL 的 /*! … */，见 _executable_comment）；
    - 写成 ! 的 NOT：MySQL 里 ! 比比较运算结合得紧（! status = 'paid' 是 (!status) = 'paid'），NOT 比比较结合得松，
      sqlglot 都按 NOT 读；
    - 类型名：解析后重新生成换了名字、又不是这个方言里的同义写法（SQLite 的 CAST(x AS STRING) 是数值亲和，解析成
      和 CAST(x AS TEXT) 一样的节点）；
    - 已知会被读错的写法（MySQL 字符串里的反斜杠转义、1_000 这类数字、没加括号地混用的比较 / 按位运算、PostgreSQL 写成
      符号的匹配运算符和 || 混用、前缀的 @、|/、XOR，不知道是哪种库时套着的块注释、紧跟着字的 --、双引号括起来的名字，
      见 _misread）。"""
    try:
        tokens = _tokens(text, read)
    except Exception:  # noqa: BLE001 - 切词都不行的，解析也过不了，那边会说
        return False
    if _executable_comment(tokens):
        return True
    why = _misread(tokens, text, read)
    if why:
        logger.debug("SQL 有 sqlglot 会读错的写法（%s），不在语法树上判等价", why)
        return True
    prev = None
    for token in tokens:
        kind = token.token_type.name
        if kind in _LOSSY_LITERALS or (kind == "NOT" and token.text == "!"):
            return True
        if kind == "PLUS":
            if prev is None:
                return True
            pk, word = prev.token_type.name, prev.text
            operand = pk in ("VAR", "IDENTIFIER", "NUMBER", "R_PAREN", "R_BRACKET", "NULL", "TRUE", "FALSE", "END",
                             "STAR") or pk.endswith("STRING") or (
                _PLAIN_NAME.match(word) is not None and word.upper() not in _OPERAND_KEYWORDS)
            if not operand:
                return True
        prev = token
    try:
        again = _tokens(tree.sql(dialect=read, comments=False), read)
    except Exception:  # noqa: BLE001 - 重新生成不了：当成丢了写法
        return True
    return _type_words(tokens, read) != _type_words(again, read)


def _mark_comma_joins(tree: Any, text: str, read: str | None) -> None:
    """SQLite 里逗号连接和 CROSS JOIN 解析成同一个节点（都是 kind=CROSS），可 SQLite 不调换 CROSS JOIN 两边的先后、
    逗号连接会调换：结果看行出来的先后时（GROUP_CONCAT、有并列的 LIMIT）两种写法不一样。按原文认出哪些是逗号连接，
    去掉它们的 CROSS（和别的方言解析出来的一样）。认不出时整条语句的 CROSS 连接标成认不出（见 _fingerprint）。"""
    from sqlglot import exp

    joins = [j for j in tree.find_all(exp.Join) if str(j.args.get("kind") or "").upper() == "CROSS"]
    if not joins:
        return
    try:
        tokens = _tokens(text, read)
    except Exception:  # noqa: BLE001
        tokens = []
    marks: list[tuple[int, bool]] = []
    depth = 0
    in_from: set[int] = set()
    for tok in tokens:
        name = tok.token_type.name
        if name == "L_PAREN":
            depth += 1
        elif name == "R_PAREN":
            in_from.discard(depth)
            depth -= 1
        elif name == "FROM":
            in_from.add(depth)
        elif name in _CLAUSE_ENDS:
            in_from.discard(depth)
        elif name == "COMMA" and depth in in_from:
            marks.append((tok.start, True))
        elif name in ("JOIN", "STRAIGHT_JOIN"):
            marks.append((tok.start, False))
    for join in joins:
        starts = [i.meta["start"] for i in join.this.find_all(exp.Identifier) if isinstance(i.meta.get("start"), int)]
        before = [m for m in marks if starts and m[0] < min(starts)]
        if not before:
            join.meta["sql_semantics_join"] = f"unknown:{id(tree)}"
            continue
        comma = max(before)[1]
        if comma:
            join.set("kind", None)


@dataclass
class _Form:
    """一条语句在某一级的规范形式。key 一样才算等价，text 是生成的 SQL（给人看）；pre 是起规范名字之前的树。"""

    level: str
    text: str
    tree: Any
    pre: Any = None
    qualified: bool = False

    @property
    def key(self) -> str:
        """比等价用的写法：语法树逐个节点的类型和参数（见 _fingerprint）。不拿生成的 SQL 比：sqlglot 按方言生成时
        会把不同的写法写成同一个样子（SQLite 的 CAST(x AS DATE) 生成 DATE(x)，两者在 SQLite 里结果不同）。"""
        return _fingerprint(self.tree)


def _fingerprint(node: Any) -> str:
    """语法树的确定写法：每个节点写类名和全部参数（按参数名排序，字符串原样），不同的树一定写得不一样。括号不算
    （语法树的结构已经定了怎么结合：SQLite 里 (a) 和 a 的类型亲和也一样）；类型标注、注释、meta 不算，只有认不出
    是逗号还是 CROSS JOIN 的连接带上标记（见 _mark_comma_joins）。"""
    from sqlglot import exp

    if isinstance(node, exp.Paren):
        return _fingerprint(node.this)
    if isinstance(node, exp.Expr):
        parts = [f"{k}={_fingerprint(v)}" for k, v in sorted(node.args.items())
                 if v is not None and v is not False and v != []]
        mark = node.meta.get("sql_semantics_join") if isinstance(node, exp.Join) else None
        if mark:
            parts.append(f"@join={mark}")
        surface = node.meta.get(_SURFACE)
        if surface:
            parts.append(f"@surface={surface}")
        return f"{type(node).__name__}({','.join(parts)})"
    if isinstance(node, list):
        return "[" + ",".join(_fingerprint(v) for v in node) + "]"
    return repr(node)


def _key(node: Any, read: str | None = None) -> tuple[str, str]:
    """排序、去重用的键：只看语法树（见 _fingerprint），括号写没写不影响先后。"""
    return _fingerprint(node), ""


@dataclass
class _Schema:
    """数据源的表结构：mapping 给 qualify（只要列名），types 记各列声明的类型（判断「相等的值是不是写法也一样」，见
    _Sensitivity），columns 是所有表的列名（小写）；not_null 是确定不会为空的列（声明了 NOT NULL，或者是主键：
    PostgreSQL / MySQL 的主键列都不为空，SQLite 只有单独一列、声明成 INTEGER 的主键是 rowid、不会为空——别的主键
    在 SQLite 里照样能存空值），pk 是各表的主键列，unique 是唯一约束 / 唯一索引的列组，fks 是外键（列, 指向的表,
    指向的列）。

    约束（not_null、pk、unique、fks）只拿来让差异说明少说（表结构说某处不同不起作用就不说），不拿来改写、判等价：类型
    亲和、COLLATE、部分唯一索引、MySQL 的隐式转换都会让约束说的「唯一」「对得上」不成立（见 _finish）。"""

    mapping: dict[str, dict[str, str]] | None = None
    types: dict[str, dict[str, str]] = field(default_factory=dict)
    columns: set[str] = field(default_factory=set)
    not_null: dict[str, set[str]] = field(default_factory=dict)
    pk: dict[str, list[str]] = field(default_factory=dict)
    unique: dict[str, list[list[str]]] = field(default_factory=dict)
    fks: dict[str, list[tuple[list[str], str, list[str]]]] = field(default_factory=dict)


def _schema_info(schema: Mapping[str, Any] | None) -> _Schema:
    """schema_cache → _Schema。qualify 用的类型一律记 UNKNOWN：只要列名（认列属于哪张表、展开 SELECT *）；按类型做
    的改写和方言有关，不做。nullable 为 False 的列、主键列另记下来（哪些主键列确定不为空和方言有关，见
    _Schema.not_null 和 _not_null_column）。"""
    tables = schema.get("tables") if isinstance(schema, Mapping) else None
    if not isinstance(tables, Mapping):
        return _Schema()
    mapping: dict[str, dict[str, str]] = {}
    types: dict[str, dict[str, str]] = {}
    not_null: dict[str, set[str]] = {}
    pk: dict[str, list[str]] = {}
    unique: dict[str, list[list[str]]] = {}
    fks: dict[str, list[tuple[list[str], str, list[str]]]] = {}
    for name, meta in tables.items():
        columns = meta.get("columns") if isinstance(meta, Mapping) else None
        cols = [c for c in columns or [] if isinstance(c, Mapping) and isinstance(c.get("name"), str) and c["name"]]
        if cols:
            key = str(name).translate(_ASCII_LOWER)
            mapping[str(name)] = {c["name"]: "UNKNOWN" for c in cols}
            types[key] = {c["name"].translate(_ASCII_LOWER): str(c.get("type") or "") for c in cols}
            not_null[key] = {c["name"].translate(_ASCII_LOWER) for c in cols if c.get("nullable") is False}
            keys = meta.get("primary_key") if isinstance(meta, Mapping) else None
            if isinstance(keys, list) and all(isinstance(k, str) for k in keys):
                pk[key] = [k.translate(_ASCII_LOWER) for k in keys]
            groups = meta.get("unique") if isinstance(meta, Mapping) else None
            if isinstance(groups, list):
                unique[key] = [[c.translate(_ASCII_LOWER) for c in g] for g in groups
                               if isinstance(g, list) and g and all(isinstance(c, str) for c in g)]
            refs = meta.get("foreign_keys") if isinstance(meta, Mapping) else None
            for fk in refs if isinstance(refs, list) else []:
                if not isinstance(fk, Mapping) or not isinstance(fk.get("to_table"), str):
                    continue
                cols = [c.translate(_ASCII_LOWER) for c in fk.get("columns") or [] if isinstance(c, str)]
                to = [c.translate(_ASCII_LOWER) for c in fk.get("to_columns") or [] if isinstance(c, str)]
                if cols:
                    fks.setdefault(key, []).append((cols, fk["to_table"].translate(_ASCII_LOWER), to))
    return _Schema(mapping or None, types, {c for cols in types.values() for c in cols}, not_null, pk, unique, fks)


def _not_null_column(info: _Schema, table: str, column: str, read: str | None) -> bool:
    """表结构里确定不会为空的列：声明了 NOT NULL；或者是主键——PostgreSQL / MySQL 的主键列都不为空，SQLite 只有单独
    一列、声明的类型正好是 INTEGER 的主键（rowid 的别名）不为空，别的主键因为历史原因能存空值。"""
    t, c = table.translate(_ASCII_LOWER), column.translate(_ASCII_LOWER)
    if c in info.not_null.get(t, set()):
        return True
    keys = info.pk.get(t) or []
    if c not in keys:
        return False
    if read == "sqlite":
        return len(keys) == 1 and info.types.get(t, {}).get(c, "").strip().upper() == "INTEGER"
    return read in ("postgres", "mysql")


def _canonical_forms(root: Any, read: str | None, info: _Schema) -> dict[str, _Form]:
    """一条语句的几级规范形式：

    - optimizer：认列（sqlglot 的 qualify：认清每一列属于哪个来源，给了表结构就展开 SELECT *）之后的全套改写（见
      _optimized、_finish）。认不全列、有和列别名同名又可能是表的列的名字、不确定的函数、递归 CTE、LATERAL 之类的
      不出这一级。
    - light：不认列，只做不需要认列的改写（见 _light）。
    - parsed：解析后只统一名字的大小写、引号（见 _normalize_names），总是有。

    每一级的每一步都只做保持语义的变换，某一级出不来就没有这一级，从不抛异常。"""
    out: dict[str, _Form] = {}
    for level, build in ((_OPTIMIZER, _optimized), (_LIGHT, _light)):
        try:
            out[level] = build(root, read, info)
        except _Unsupported as e:
            logger.debug("SQL 规范化（%s）跳过：%s", level, e)
        except Exception as e:  # noqa: BLE001 - sqlglot 认不了列、写法不支持：这一级不出，别的级照样比
            from sqlglot.errors import SqlglotError

            if isinstance(e, SqlglotError):
                logger.debug("SQL 规范化（%s）跳过：%s", level, e)
            else:
                logger.warning("SQL 规范化（%s）失败", level, exc_info=True)
    try:
        tree = root.copy()
        _normalize_names(tree, read, info)
        out[_PARSED] = _Form(_PARSED, tree.sql(dialect=read, comments=False), tree)
    except Exception:  # noqa: BLE001
        logger.warning("SQL 重新生成失败", exc_info=True)
    return out


def _optimized(root: Any, read: str | None, info: _Schema) -> _Form:
    """认列之后的规范形式：统一名字 → 列名表写进定义 → USING 写成 ON → FETCH 写成 LIMIT → qualify 认列（给了表结构
    先按表结构，表结构里没有的列再不给表结构认一遍）→ 来源别名改成整条语句里互不相同的 → 只引用一次的 CTE 写回用到
    它的地方 → 只筛行、透列的子查询并进外层 → 去掉没用的 CTE → 两级共用的几步（_finish）。"""
    from sqlglot.errors import OptimizeError
    from sqlglot.optimizer.qualify import qualify

    tree = root.copy()
    why = _unsupported(tree, read) or ("不确定的函数" if _volatile(tree) else None) or \
        ("派生来源的列同名" if _duplicate_outputs(tree) else None) or \
        ("别名被子查询遮住" if _shadowed_reference(tree, info) else None)
    if why:
        raise _Unsupported(why)
    _normalize_names(tree, read, info)
    _remember_names(tree)
    _mark_unsure(tree)
    _push_column_lists(tree)
    _expand_using(tree)
    _qualify_stars(tree)
    _fetch_to_limit(tree)
    _normalize_negation(tree)
    qualified = None
    for mapping in ([info.mapping, None] if info.mapping else [None]):
        if _alias_or_column(tree, read, mapping):
            continue
        try:
            qualified = qualify(tree.copy(), dialect=read, schema=mapping, validate_qualify_columns=True,
                                quote_identifiers=False, identify=False, expand_stars=True, expand_alias_refs=True)
            break
        except OptimizeError as e:
            logger.debug("SQL 认列失败：%s", e)
    if qualified is None:
        raise _Unsupported("认不全列")
    tree = qualified
    if _duplicate_outputs(tree):
        raise _Unsupported("派生来源的列同名（展开 * 之后）")
    _split_simple_ctes(tree, read, info)
    _inline_ctes(tree)
    # 写回的 CTE 定义各是一份副本，里面的别名一样：同一个 CTE 写回外层和相关子查询两处，并进去之后子查询里本来指外层
    # 的列就被里层同名的别名遮住了。写回之后再把整条语句的别名改成互不相同的
    _unique_aliases(tree)
    _merge_derived(tree, read, info)
    _push_into_groups(tree, read, info)
    _drop_unused_ctes(tree)
    return _finish(tree, read, info, level=_OPTIMIZER, qualified=True)


def _light(root: Any, read: str | None, info: _Schema) -> _Form:
    """不认列的规范形式：统一名字、列名表写进定义、USING 写成 ON、FETCH 写成 LIMIT、只引用一次的 CTE 写回用到它的
    地方，再走 _finish 里不需要认列的几步。"""
    tree = root.copy()
    why = _unsupported(tree, read) or ("不确定的函数" if _volatile(tree) else None) or \
        ("别名被子查询遮住" if _shadowed_reference(tree, info) else None)
    if why:
        raise _Unsupported(why)
    _normalize_names(tree, read, info)
    _remember_names(tree)
    _mark_unsure(tree)
    _push_column_lists(tree)
    _expand_using(tree)
    _qualify_stars(tree)
    _fetch_to_limit(tree)
    _normalize_negation(tree)
    _inline_ctes(tree)
    _drop_unused_ctes(tree)
    return _finish(tree, read, info, level=_LIGHT, qualified=False)


def _finish(tree: Any, read: str | None, info: _Schema, *, level: str, qualified: bool) -> _Form:
    """两级共用的几步，都只做保持语义的变换（为什么保持语义见各函数的说明），按这个先后：

    0. 原样选出 FROM 里一个子查询全部列、什么别的都不做的一段换成那个子查询（_unwrap_passthrough）。
    1. 动来源的：两个来源的 RIGHT JOIN 写成 LEFT JOIN；WHERE 把外连接可空一侧没关联上的行筛掉了的写成内连接；只取可空
       一侧跳过空值的聚合时 LEFT JOIN 写成内连接；开头那组内连接的条件挪进 WHERE、写成逗号连接；恒为真的条件、同一组
       AND 里另一项已经保证不为空的 IS NOT NULL 去掉；没用到、又不怕重复的 LEFT JOIN 去掉；（认列之后）按关联条件
       相等、类型一样的两列换成同一列。结果看行出来的先后的一段（见 _Sensitivity）这些都不做——只有挪内连接的 ON
       条件、去掉 1 = 1 这类常量条件照做（库里一视同仁，扫描的先后不变）。表结构里的约束（主键、唯一约束、NOT NULL）
       一律不拿来改写：SQLite 的类型亲和（TEXT 主键对数值列，'1' 和 '01' 都等于 1）、比较写了 COLLATE、部分唯一索引
       （只管没删的行，探查照样记成 unique）、MySQL 拿字符串和数值比时的隐式转换都会让「唯一」不成立，表结构里看不全，
       宁可认不出。
    2. 只看有没有、在不在的子查询：EXISTS 写成 SELECT 1，IN 子查询里不影响取值集合的去重、分组、排序、UNION 去掉。
    3. 去重的：SELECT DISTINCT 写成按选出来的列 GROUP BY、已经按选出来的列分组的 DISTINCT 去掉（只在「相等」就是
       「一样」、不看行出来的先后时做）。
    4. 和来源无关的：COUNT(1) 写成 COUNT(*)、MIN / MAX 里的 DISTINCT 去掉、IIF / IF 写成 CASE；IN 列表去重排序、只有一个
       常量的写成 =；BETWEEN 写成两个比较；NOT 套比较写成反过来的比较、NOT NOT 去掉；LIMIT 的同义写法；不影响结合的
       括号、ORDER BY 的 ASC、OFFSET 0、重复的排序键去掉；最外层 ORDER BY 引用的别名换成表达式，最外层和条件里的子查询
       选出来的列名去掉。
    5. 起规范的名字、排好交换律成立的地方（见 _named）。
    """
    tree = _unwrap_passthrough(tree, read)
    sens = _Sensitivity(tree, read, info)
    _right_to_left(tree, sens)
    sens = _Sensitivity(tree, read, info)
    _fold_not(tree, sens)
    _reject_nulls(tree, read, sens)
    _null_blind_left_joins(tree, read, sens)
    _flatten_prefix(tree, sens, read)
    _drop_true_conjuncts(tree)
    _drop_implied_not_null(tree, read, sens)
    _drop_unused_left_joins(tree, sens)
    if qualified:
        _substitute_equal_columns(tree, read, info, sens)
    _exists_subqueries(tree)
    sens = _Sensitivity(tree, read, info)
    _membership_subqueries(tree, sens)
    _distinct_to_group(tree, sens)
    _count_star(tree)
    _minmax_distinct(tree, sens)
    _case_forms(tree, read)
    _in_lists(tree)
    _expand_between(tree, read, sens)
    _fold_not(tree, sens)
    _normalize_limits(tree, read)
    _strip_parens(tree)
    _strip_asc(tree)
    _zero_offset(tree)
    _order_by_aliases(tree)
    _dedupe_order(tree, sens)
    _drop_output_aliases(tree)
    pre = tree.copy()
    sens = _Sensitivity(tree, read, info)
    tree = _named(tree, read, qualified, sens)
    return _Form(level, tree.sql(dialect=read, comments=False), tree, pre, qualified)


def _named(tree: Any, read: str | None, qualified: bool, sens: "_Sensitivity", hint: "_Hint | None" = None) -> Any:
    """起规范的名字、排好交换律成立的地方，最后统一引号（只给需要的标识符加）。"""
    from sqlglot.optimizer.qualify_columns import quote_identifiers

    _Namer(tree, read, qualified, sens, hint).canon(tree, 0, {})
    return quote_identifiers(tree, dialect=read, identify=False)


# ---- 守门：哪些结构不走规范化的两级

def _volatile(tree: Any) -> bool:
    from sqlglot import exp

    for node in tree.walk():
        if isinstance(node, (exp.Rand, exp.Randn)) or type(node).__name__.lower() in _VOLATILE:
            return True
        if isinstance(node, exp.Anonymous) and str(node.name).lower() in _VOLATILE:
            return True
    return False


def _unsupported(tree: Any, read: str | None) -> str | None:
    """规范化的两级不处理的结构：递归 CTE（写了 RECURSIVE，或者 CTE 引用自己、引用后面的 CTE——SQLite 里不写
    RECURSIVE 也是递归）、LATERAL、UNNEST、PIVOT、表函数、VALUES、SEMI / ANTI / ASOF / NATURAL 这些连接、实表别名
    带列名（PostgreSQL 的 orders AS o(a, b) 给列改了名）、BETWEEN SYMMETRIC、FETCH … WITH TIES / PERCENT、ROLLUP /
    CUBE / GROUPING SETS、SELECT INTO、加锁。"""
    from sqlglot import exp

    for node in tree.walk():
        if isinstance(node, exp.With):
            if node.args.get("recursive"):
                return "递归 CTE"
            names = [c.alias_or_name for c in node.expressions]
            for i, cte in enumerate(node.expressions):
                later = set(names[i:])
                if any(t.name in later and not t.args.get("db") for t in cte.this.find_all(exp.Table)):
                    return "CTE 引用自己或后面的 CTE"
        if isinstance(node, (exp.Lateral, exp.Unnest, exp.Pivot, exp.TableSample, exp.Values, exp.Into, exp.Lock)):
            return type(node).__name__
        if isinstance(node, exp.Table):
            if not isinstance(node.this, exp.Identifier):
                return "表函数"
            alias = node.args.get("alias")
            if isinstance(alias, exp.TableAlias) and alias.columns:
                return "实表别名带列名"
        if isinstance(node, exp.Join) and (node.args.get("method") or str(node.kind or "").upper() in
                                           ("SEMI", "ANTI", "ASOF", "POSITIONAL", "STRAIGHT_JOIN", "ARRAY")):
            return "连接方式"
        if isinstance(node, exp.Between) and node.args.get("symmetric"):
            return "BETWEEN SYMMETRIC"
        if isinstance(node, exp.Fetch):
            options = node.args.get("limit_options")
            if options is not None and (options.args.get("percent") or options.args.get("with_ties")):
                return "FETCH WITH TIES / PERCENT"
        if isinstance(node, exp.Group) and any(node.args.get(k) for k in ("rollup", "cube", "grouping_sets", "totals")):
            return "ROLLUP / CUBE / GROUPING SETS"
    return None


def _duplicate_outputs(tree: Any) -> bool:
    """FROM 里的子查询、CTE 选出来的列有同名的（SELECT user_id AS k, SUM(amount) AS k …）：外面按名字引用 k 时 SQLite
    认第一个、PostgreSQL / MySQL 报错，按名字找定义的改写（外层条件挪进 HAVING、并子查询、展开 *）都可能找错。有就
    不走认列那一级。"""
    from sqlglot import exp

    for holder in tree.find_all(exp.CTE, exp.Subquery):
        if isinstance(holder, exp.Subquery) and not isinstance(holder.parent, (exp.From, exp.Join)):
            continue
        leaves = _leaves(holder.this)
        if leaves and isinstance(leaves[0], exp.Select):
            names = [p.alias_or_name for p in leaves[0].expressions if p.alias_or_name and not _is_star(p)]
            if len(names) != len(set(names)):
                return True
    return False


def _shadowed_reference(tree: Any, info: _Schema) -> bool:
    """子查询里写了表名（别名）的列，而这个别名在子查询和外层各指一个不同的来源（FROM orders o … EXISTS (SELECT 1 FROM
    refunds AS o WHERE o.status = 'paid')）：SQLite 先在里层的 o 里找 status，找不到就往外层找（真实的 SQLite 上
    试过），sqlglot 一律认里层的。里层是实表、表结构里有这一列（或者是派生来源、选出来的列里有它）时才确定是里层的；
    认不准就不走规范化的两级。同一张表在里外层用同一个别名的（FROM orders WHERE … (SELECT … FROM orders)）不算：
    里外层的列一样。"""
    from sqlglot import exp

    def signature(item: Any, ctes: dict[str, Any]) -> Any:
        if isinstance(item, exp.Table) and isinstance(item.this, exp.Identifier):
            key = item.name.translate(_ASCII_LOWER)
            if not item.args.get("db") and key in ctes:
                return "cte", id(ctes[key])
            return "table", ".".join(p for p in (item.catalog, item.db, item.name) if p).translate(_ASCII_LOWER)
        return "derived", id(item)

    def certain(item: Any, column: str, ctes: dict[str, Any]) -> bool:
        kind, _ = signature(item, ctes)
        if kind == "table":
            columns = info.types.get(item.name.translate(_ASCII_LOWER))
            return columns is not None and column.translate(_ASCII_LOWER) in columns
        body = item.this if isinstance(item, exp.Subquery) else ctes[item.name.translate(_ASCII_LOWER)].this
        return any(n is not None and n == column for n in _output_names(body))

    def walk(query: Any, frames: list[dict[str, Any]], ctes: dict[str, Any]) -> bool:
        if isinstance(query, exp.Subquery):
            return walk(query.this, frames, ctes)
        with_ = query.args.get("with_")
        if with_ is not None:
            ctes = dict(ctes)
            for cte in with_.expressions:
                if walk(cte.this, frames, ctes):
                    return True
                ctes[cte.alias_or_name.translate(_ASCII_LOWER)] = cte
        if isinstance(query, exp.SetOperation):
            return walk(query.left, frames, ctes) or walk(query.right, frames, ctes)
        if not isinstance(query, exp.Select):
            return False
        frame = {item.alias_or_name: item for item in _items_of(query) if item.alias_or_name}
        for item in _items_of(query):
            if isinstance(item, exp.Subquery) and walk(item.this, frames, ctes):
                return True
        stack = frames + [frame]
        for node in _block_nodes(query):
            if isinstance(node, exp.Column) and node.table:
                defs = [f[node.table] for f in stack if node.table in f]
                if len({signature(d, ctes) for d in defs}) > 1 and not certain(defs[-1], node.name, ctes):
                    return True
        return any(walk(sub, stack, ctes) for sub in _nested_queries(query))

    return walk(tree, [], {})


# ---- 名字

def _identifier_role(ident: Any) -> str:
    """一个标识符在语法树里是什么：column（列名）/ column_alias（列的别名、CTE 和子查询的列名表）/ table（表名、
    列前面的表名或别名）/ table_alias（表、子查询、CTE 的别名）/ other。"""
    from sqlglot import exp

    parent, key = ident.parent, ident.arg_key
    if isinstance(parent, exp.Column):
        return "column" if key == "this" else "table"
    if isinstance(parent, exp.Table):
        return "table"
    if isinstance(parent, exp.TableAlias):
        return "column_alias" if key == "columns" else "table_alias"
    if isinstance(parent, exp.Alias):
        return "column_alias"
    return "other"


def _visible_ctes(node: Any) -> dict[str, Any]:
    """node 所在位置看得见的 CTE（名字 → CTE），里层的遮住外层的。"""
    from sqlglot import exp

    chain = []
    cur = node
    while cur is not None:
        with_ = cur.args.get("with_") if isinstance(cur, exp.Expr) else None
        if with_ is not None:
            chain.append(with_)
        cur = cur.parent
    out: dict[str, Any] = {}
    for with_ in reversed(chain):
        for cte in with_.expressions:
            out[cte.alias_or_name.translate(_ASCII_LOWER)] = cte
    return out


def _column_in_scope(col: Any, name: str, info: _Schema) -> bool:
    """SQLite 里不带表名的名字 name（已转小写）在 col 所在的这一段、外层各段的来源里确实是一列：实表在表结构里、有这一
    列；CTE、FROM 里的子查询选出来的列（或者写的列名表）里有这个名字。认不出来源的列（没给表结构、选的是 *）一律当成
    不知道（返回 False）：调用方就原样留着，不会把可能是字符串的 "State" 和 "state" 认成同一列。"""
    from sqlglot import exp

    ctes = _visible_ctes(col)
    node = col.parent
    while node is not None:
        if isinstance(node, exp.Select):
            for item in _items_of(node):
                body = None
                if isinstance(item, exp.Subquery):
                    body = item.this
                elif isinstance(item, exp.Table) and not item.args.get("db") and \
                        item.name.translate(_ASCII_LOWER) in ctes:
                    body = ctes[item.name.translate(_ASCII_LOWER)].this
                if body is not None:
                    names = _output_names(body)
                    if any(n is not None and n.translate(_ASCII_LOWER) == name for n in names):
                        return True
                    continue
                if isinstance(item, exp.Table):
                    columns = info.types.get(item.name.translate(_ASCII_LOWER))
                    if columns is not None and name in columns:
                        return True
        node = node.parent
    return False


def _normalize_names(tree: Any, read: str | None, info: _Schema) -> None:
    """按方言的规则统一名字的大小写和引号，只做库里认成同一个名字的改写；处理过的标识符标成 case_sensitive，后面
    qualify 不再按 sqlglot 的规则改（sqlglot 认为 SQLite 带引号的名字也不分大小写，可 SQLite 找不到这一列时把双引号
    里的当成字符串，'Paid' 和 'paid' 就不同了）。只认 ASCII 字母的大小写（库里也只折 ASCII）。

    - SQLite：不带引号的名字、带引号的表名和别名不分大小写，统一成小写；条件、SELECT 列表里不带表名、带引号的列名
      （"Paid"）原样留着，除非它所在的这一段或外层各段的来源里确实有这一列（见 _column_in_scope：找得到这一列就不会被
      当成字符串）。只看这条语句用到的表：表结构里别的表有同名的列不算（orders 没有 state 列时 "State" 是字符串，
      refunds 有 state 列不相干）。
    - PostgreSQL（和认不出的方言）：不带引号的名字折成小写；带引号的里面没有大写字母就和不带引号的是同一个名字。
    - MySQL：列名、列的别名不分大小写，统一成小写；表名、表的别名在 Linux 上分大小写，原样。
    """
    from sqlglot import exp

    for ident in list(tree.find_all(exp.Identifier)):
        name = ident.this
        if not isinstance(name, str):
            continue
        role = _identifier_role(ident)
        quoted = bool(ident.args.get("quoted"))
        lower = name.translate(_ASCII_LOWER)
        if read == "sqlite":
            keep = quoted and role == "column" and not ident.parent.args.get("table") and \
                not _column_in_scope(ident.parent, lower, info)
            if keep:
                ident.meta[_UNSURE] = True
            else:
                name, quoted = lower, False
        elif read == "mysql":
            if role in ("column", "column_alias"):
                name = lower
            quoted = False
        elif not quoted or lower == name:
            name, quoted = lower, False
        if quoted or not _PLAIN_NAME.match(name):
            quoted = True
        ident.set("this", name)
        ident.set("quoted", quoted)
        ident.meta["case_sensitive"] = True


def _mark_unsure(tree: Any) -> None:
    """条件里的子查询（IN、EXISTS、标量）里不带表名的列标成认不准（见 _UNSURE）：它可能是子查询里的表的列，也可能是
    外层的列，表结构里没有就分不清。只影响差异说明说不说具体的，不影响规范形式。"""
    from sqlglot import exp

    for col in tree.find_all(exp.Column):
        if col.table or not isinstance(col.this, exp.Identifier):
            continue
        node = col.parent
        while node is not None:
            if isinstance(node, exp.Select) and (_scalar_context(node) or _condition_query(node)):
                col.this.meta[_UNSURE] = True
                break
            node = node.parent


def _remember_names(tree: Any) -> None:
    """CTE、FROM 里的子查询原来的名字记在里面那段查询上（见 _ORIGINAL）。"""
    from sqlglot import exp

    for cte in tree.find_all(exp.CTE):
        cte.this.meta.setdefault(_ORIGINAL, cte.alias_or_name)
    for sub in tree.find_all(exp.Subquery):
        if isinstance(sub.parent, (exp.From, exp.Join)) and sub.alias:
            sub.this.meta.setdefault(_ORIGINAL, sub.alias)


def _push_column_lists(tree: Any) -> None:
    """CTE、FROM 里的子查询自己写了列名表的（WITH p(uid, total) AS (…)、(…) AS t(x, y)），列名写成里面那段（UNION 是
    第一支）选出来的列的别名，列名表去掉：外面按这些名字引用，两种写法是一回事。里面那段选的是 *（不知道有几列）、
    列数对不上、本段 ORDER BY / GROUP BY / HAVING 还按原来的别名引用的，没法改写，不走规范化的两级。"""
    from sqlglot import exp

    for holder in list(tree.find_all(exp.CTE, exp.Subquery)):
        alias = holder.args.get("alias")
        if not isinstance(alias, exp.TableAlias) or not alias.columns:
            continue
        body = holder.this
        target = _leaves(body)[0] if isinstance(body, exp.SetOperation) else body
        names = [c.name for c in alias.columns]
        if not isinstance(target, exp.Select) or _starry(target) or len(target.expressions) != len(names):
            raise _Unsupported("列名表对不上选出来的列")
        own = {p.alias for p in target.expressions if isinstance(p, exp.Alias) and p.alias}
        if isinstance(body, exp.SetOperation):
            if body.args.get("order") is not None:
                raise _Unsupported("合并的结果按名字排序")
        # 本段的 WHERE、ON、GROUP BY、HAVING、ORDER BY、子查询里按名字引用原来的列别名、或者正好叫新名字的（SQLite 的
        # WHERE 认列别名：WITH t(a, b) AS (SELECT amount AS b, id AS a FROM orders WHERE b > 50) 里的 b 是 amount），
        # 改名之后就指到别的列上了
        if _alias_refs(target) & (own | set(names)):
            raise _Unsupported("列名表改名之后本段的引用对不上")
        target.set("expressions", [exp.alias_(p.unalias().copy() if isinstance(p, exp.Alias) else p.copy(), n,
                                              copy=False) for p, n in zip(target.expressions, names)])
        alias.set("columns", None)


def _expand_using(tree: Any) -> None:
    """JOIN … USING (c) 写成 ON 左表.c = 右表.c，这一段里不带表名的 c 写成左表.c（内连接、LEFT JOIN 时合并出来的 c
    就是左表的 c）。只改得了「FROM 只有一个来源、第一个连接就是 USING、SELECT 里没有 *」的写法：别的（左边好几个
    来源时 c 是哪一张的、SELECT * 列的先后、RIGHT / FULL JOIN 合并出来的值、子查询里引用 c）没法照原样改写，不走
    规范化的两级。"""
    from sqlglot import exp

    for select in list(tree.find_all(exp.Select)):
        joins = select.args.get("joins") or []
        if not any(j.args.get("using") for j in joins):
            continue
        if len(joins) < 1 or not joins[0].args.get("using") or any(j.args.get("using") for j in joins[1:]):
            raise _Unsupported("USING")
        join = joins[0]
        if str(join.side or "").upper() not in ("", "LEFT") or join.args.get("on") is not None or \
                any(_is_star(p) for p in select.expressions):
            raise _Unsupported("USING")
        left, right = select.args["from_"].this, join.this
        la, ra = left.alias_or_name, right.alias_or_name
        if not la or not ra:
            raise _Unsupported("USING")
        names = [u.name for u in join.args["using"]]
        for sub in _nested_queries(select):
            if any(not c.table and c.name in names for c in sub.find_all(exp.Column)):
                raise _Unsupported("子查询里引用 USING 的列")
        # 有列别名和 USING 的列同名：ORDER BY（SQLite 的 WHERE、GROUP BY、HAVING 也可能）里单独一个名字先认列别名，
        # SELECT -o.id AS id … USING (id) ORDER BY id 是按 -o.id 排，不能写成左表的 id
        if any(isinstance(p, exp.Alias) and p.alias in names for p in select.expressions):
            raise _Unsupported("列别名和 USING 的列同名")
        for node in list(_block_nodes(select)):
            if isinstance(node, exp.Column) and not node.table and node.name in names:
                node.set("table", exp.to_identifier(la))
        conds = [exp.EQ(this=exp.column(n, table=la), expression=exp.column(n, table=ra)) for n in names]
        join.set("using", None)
        join.set("on", _and_all(conds))


def _qualify_stars(tree: Any) -> None:
    """几个来源的 SELECT * 写成按 FROM 的先后逐个来源的 t.*：两种写法是一回事（没有 USING / NATURAL 时 * 就是各来源的
    全部列依次排开）。sqlglot 的 qualify 展开 * 时把实表的列排在 FROM 里的子查询前面，和库里的先后不一样，先写成 t.*
    它就按写的先后展开。有来源没有名字的不动（这时也不走规范化的两级）。"""
    from sqlglot import exp

    for select in list(tree.find_all(exp.Select)):
        if not any(isinstance(p, exp.Star) for p in select.expressions):
            continue
        items = _items_of(select)
        if len(items) < 2 or any(not i.alias_or_name for i in items):
            continue
        stars = [exp.Column(this=exp.Star(), table=exp.to_identifier(i.alias_or_name)) for i in items]
        new: list[Any] = []
        for p in select.expressions:
            new += [c.copy() for c in stars] if isinstance(p, exp.Star) else [p]
        select.set("expressions", new)


def _fetch_to_limit(tree: Any) -> None:
    """FETCH FIRST n ROWS ONLY（不带 WITH TIES / PERCENT）就是 LIMIT n（sqlglot 把 FETCH 放在 limit 这个参数上）。"""
    from sqlglot import exp

    for query in list(tree.find_all(exp.Select, exp.SetOperation)):
        for key in ("limit", "fetch"):
            fetch = query.args.get(key)
            if not isinstance(fetch, exp.Fetch):
                continue
            count = fetch.args.get("count") or exp.Literal.number(1)
            query.set(key, None)
            query.set("limit", exp.Limit(expression=count.copy()))


# ---- 认列属于哪个来源

class _Resolver:
    """按别名认清每一列属于哪个来源（相关子查询里的列到外层找）：refs[id(来源)] 是引用它的列，cte_of[id(表)] 是它
    引用的 CTE，owner[id(来源)] 是来源所在的那段 SELECT。不带表名的列不认（轻量一级）。"""

    def __init__(self, tree: Any) -> None:
        self.refs: dict[int, list[Any]] = {}
        self.cte_of: dict[int, Any] = {}
        self.owner: dict[int, Any] = {}
        self.source_of: dict[int, Any] = {}
        self._walk(tree, [], {})

    def _walk(self, query: Any, frames: list[dict[str, Any]], ctes: dict[str, Any]) -> None:
        from sqlglot import exp

        if isinstance(query, exp.Subquery):
            self._walk(query.this, frames, ctes)
            return
        with_ = query.args.get("with_")
        if with_ is not None:
            ctes = dict(ctes)
            for cte in with_.expressions:
                self._walk(cte.this, frames, ctes)
                ctes[cte.alias_or_name] = cte
        if isinstance(query, exp.SetOperation):
            self._walk(query.left, frames, ctes)
            self._walk(query.right, frames, ctes)
            return
        if not isinstance(query, exp.Select):
            raise _Unsupported(type(query).__name__)
        frame: dict[str, Any] = {}
        for item in _items_of(query):
            if isinstance(item, exp.Subquery):
                self._walk(item.this, frames, ctes)
            elif isinstance(item, exp.Table) and isinstance(item.this, exp.Identifier):
                if not item.args.get("db") and item.name in ctes:
                    self.cte_of[id(item)] = ctes[item.name]
            else:
                raise _Unsupported(type(item).__name__)
            name = item.alias_or_name
            if not name or name in frame:
                raise _Unsupported("来源没有名字或重名")
            frame[name] = item
            self.owner[id(item)] = query
            self.refs.setdefault(id(item), [])
        stack = frames + [frame]
        for node in _block_nodes(query):
            if isinstance(node, exp.Column) and node.table:
                for f in reversed(stack):
                    if node.table in f:
                        self.refs[id(f[node.table])].append(node)
                        self.source_of[id(node)] = f[node.table]
                        break
        for sub in _nested_queries(query):
            self._walk(sub, stack, ctes)

    def body(self, item: Any) -> Any:
        """派生来源里面那段查询（FROM 里的子查询、引用的 CTE）；实表是 None。"""
        from sqlglot import exp

        if isinstance(item, exp.Subquery):
            return item.this
        cte = self.cte_of.get(id(item))
        return cte.this if cte is not None else None


def _unique_aliases(tree: Any) -> None:
    """（认列之后）整条语句里每个来源的别名改成互不相同的（_s1、_s2…），引用它的列跟着改：按原来的别名认清每一列
    属于哪个来源之后再改，相关子查询里引用外层的列不会认错；之后并子查询、写回 CTE 时名字不会撞上。"""
    from sqlglot import exp

    resolver = _Resolver(tree)
    taken = {t.name for t in tree.find_all(exp.Table)} | {c.alias_or_name for c in tree.find_all(exp.CTE)}
    n = 0
    for item in [i for s in tree.find_all(exp.Select) for i in _items_of(s)]:
        n += 1
        name = f"_s{n}"
        while name in taken:
            name += "_"
        alias = item.args.get("alias")
        if isinstance(alias, exp.TableAlias):
            alias.set("this", exp.to_identifier(name))
        else:
            item.set("alias", exp.TableAlias(this=exp.to_identifier(name)))
        for col in resolver.refs.get(id(item), []):
            col.set("table", exp.to_identifier(name))


def _nested_with(tree: Any) -> bool:
    from sqlglot import exp

    top = tree.args.get("with_")
    return any(isinstance(n, exp.With) and n is not top for n in tree.walk())


def _inline_ctes(tree: Any) -> None:
    """只引用一次的非递归顶层 CTE 写回引用它的地方，成为 FROM 里的子查询（别名照旧）：写成 CTE 还是子查询是同一个
    意思。语句里还有别的 WITH 的不动（名字可能被里层的 CTE 遮住）。"""
    from sqlglot import exp

    with_ = tree.args.get("with_")
    if with_ is None or with_.args.get("recursive") or _nested_with(tree):
        return
    changed = True
    while changed and with_.expressions:
        changed = False
        resolver = _Resolver(tree)
        for cte in list(with_.expressions):
            refs = [t for t in tree.find_all(exp.Table) if resolver.cte_of.get(id(t)) is cte]
            if len(refs) != 1:
                continue
            ref = refs[0]
            sub = exp.Subquery(this=cte.this.copy(), alias=exp.TableAlias(this=exp.to_identifier(ref.alias_or_name)))
            ref.replace(sub)
            cte.pop()
            changed = True
            break
    if not with_.expressions:
        tree.set("with_", None)


def _drop_unused_ctes(tree: Any) -> None:
    """没有被引用的非递归 CTE 去掉：查询语句里没用到的 CTE 不影响结果。"""
    from sqlglot import exp

    changed = True
    while changed:
        changed = False
        resolver = _Resolver(tree)
        used = {id(c) for c in resolver.cte_of.values()}
        for with_ in list(tree.find_all(exp.With)):
            if with_.args.get("recursive"):
                continue
            for cte in list(with_.expressions):
                if id(cte) not in used:
                    cte.pop()
                    changed = True
            if not with_.expressions and isinstance(with_.parent, exp.Expr):
                with_.parent.set("with_", None)


def _mergeable(body: Any) -> bool:
    """FROM 里的子查询能不能并进外层：只从几张实表（内连接）里筛行、选列（没有分组、聚合、去重、排序、取行、窗口函数、
    UNION、WITH，选出来的列里没有子查询），并进去之后每一行、每个值都一样。"""
    from sqlglot import exp

    if not isinstance(body, exp.Select):
        return False
    if {k for k, v in body.args.items() if v} - {"expressions", "from_", "where", "joins"}:
        return False
    sources = _items_of(body)
    if not sources or any(not isinstance(i, exp.Table) for i in sources):
        return False
    if any(not _inner(j) for j in body.args.get("joins") or []):
        return False
    stars = [p for p in body.expressions if _is_star(p)]
    if stars and (len(body.expressions) != 1 or not isinstance(body.expressions[0], exp.Star) or len(sources) != 1):
        return False
    for p in body.expressions:
        if any(isinstance(n, (exp.AggFunc, exp.Window, exp.Query, exp.Subquery)) for n in p.walk()):
            return False
    names = [p.alias_or_name for p in body.expressions]
    if not stars and (not all(names) or len(set(names)) != len(names)):
        return False
    where = body.args.get("where")
    return where is None or not any(isinstance(n, (exp.AggFunc, exp.Window)) for n in where.walk())


def _merge_derived(tree: Any, read: str | None, info: _Schema) -> None:
    """（认列之后）只筛行、选列的子查询（见 _mergeable）并进外层：外层引用它的列换成里面的定义，里面的 WHERE 和外层的
    WHERE（和它里面内连接的条件）用 AND 连起来，来源换成里面那几张表。只并内连接一侧（FROM 或内连接进来的）的：外连接
    可空一侧没关联上的行该是空值，并进来就成了算出来的值。结果看行出来的先后的外层（见 _Sensitivity）不并。
    外层有 SELECT * 的：子查询选的就是 *（换成那张表，列一样）或者它是唯一的来源（* 换成它选出来的列）才并。"""
    from sqlglot import exp

    def substitute(col: Any, expr: Any) -> None:
        new = expr.copy()
        if not isinstance(new, (exp.Column, exp.Literal, exp.Null, exp.Boolean)):
            new = exp.Paren(this=new)
        col.replace(new)

    for _ in range(64):
        resolver = _Resolver(tree)
        # 浮点数求和这一条不挡并子查询：只筛行选列的子查询三种库自己也会并进外层（PostgreSQL 的 subquery pull-up、
        # MySQL 的 derived_merge、SQLite 的 flattening），并不并执行起来先后一样
        sens = _Sensitivity(tree, read, info, resolver, floats=False)
        done = False
        for select in reversed(list(tree.find_all(exp.Select))):
            if sens.of(select):
                continue
            joins = select.args.get("joins") or []
            if any(str(j.side or "").upper() in ("RIGHT", "FULL") for j in joins):
                continue
            items = _items_of(select)
            for index, item in enumerate(items):
                if not isinstance(item, exp.Subquery) or not _mergeable(item.this):
                    continue
                if index and not _inner(joins[index - 1]):
                    continue
                body = item.this
                sources = _items_of(body)
                src = sources[0]
                star_body = isinstance(body.expressions[0], exp.Star)
                defs = {} if star_body else {p.alias_or_name: p.unalias() for p in body.expressions}
                refs = resolver.refs.get(id(item), [])
                if not star_body and any(c.name not in defs for c in refs):
                    continue
                stars = [p for p in select.expressions if _is_star(p)]
                bare = [p for p in stars if isinstance(p, exp.Star)]
                if bare and not star_body and len(items) != 1:
                    continue
                alias = item.alias_or_name
                for col in refs:
                    if star_body:
                        col.set("table", exp.to_identifier(src.alias_or_name))
                    else:
                        substitute(col, defs[col.name])
                if not star_body:
                    expanded = [exp.alias_(e.copy(), n, copy=False) for n, e in defs.items()]
                    new_list = []
                    for p in select.expressions:
                        if isinstance(p, exp.Star) or (isinstance(p, exp.Column) and p.is_star and p.table == alias):
                            new_list += [e.copy() for e in expanded]
                        else:
                            new_list.append(p)
                    select.set("expressions", new_list)
                else:
                    for p in stars:
                        if isinstance(p, exp.Column) and p.table == alias:
                            p.set("table", exp.to_identifier(src.alias_or_name))
                conds = []
                inner_where = body.args.get("where")
                if inner_where is not None:
                    conds += _conjuncts(inner_where.this)
                for join in body.args.get("joins") or []:
                    if join.args.get("on") is not None:
                        conds += _conjuncts(join.args["on"])
                if index:
                    own = joins[index - 1]
                    if own.args.get("on") is not None:
                        conds += _conjuncts(own.args["on"])
                    position = index - 1
                    new_joins = joins[:position] + [exp.Join(this=s.copy()) for s in sources] + joins[position + 1:]
                    select.set("joins", new_joins)
                else:
                    select.args["from_"].set("this", src.copy())
                    select.set("joins", [exp.Join(this=s.copy()) for s in sources[1:]] + list(joins) or None)
                outer = select.args.get("where")
                conds += _conjuncts(outer.this) if outer is not None else []
                if conds:
                    select.set("where", exp.Where(this=_and_all([c.copy() for c in conds])))
                done = True
                break
            if done:
                break
        if not done:
            return


def _split_simple_ctes(tree: Any, read: str | None, info: "_Schema") -> None:
    """引用了好几次、只从几张表里筛行选列的 CTE（见 _mergeable），每处引用各写回一份 FROM 里的子查询（别名照旧）：
    这样的 CTE 是确定的（有不确定函数的语句不走这一级），算几遍结果一样，写回之后每处都能并进外层。语句里还有别的
    WITH 的不动（名字可能被里层的 CTE 遮住）；有一处引用在结果看行出来先后的一段里的不动（PostgreSQL 把引用了好几次
    的 CTE 先算出来存着，写回之后扫描的先后可能就变了）。"""
    from sqlglot import exp

    with_ = tree.args.get("with_")
    if with_ is None or with_.args.get("recursive") or _nested_with(tree):
        return
    resolver = _Resolver(tree)
    sens = _Sensitivity(tree, read, info, resolver)
    for cte in list(with_.expressions):
        if not _mergeable(cte.this):
            continue
        refs = [t for t in tree.find_all(exp.Table) if resolver.cte_of.get(id(t)) is cte]
        if len(refs) < 2 or any(r.find_ancestor(exp.CTE) is not None for r in refs) or \
                any(sens.of(resolver.owner[id(r)]) for r in refs):
            continue
        for ref in refs:
            ref.replace(exp.Subquery(this=cte.this.copy(), alias=exp.TableAlias(this=exp.to_identifier(ref.alias_or_name))))
        cte.pop()
    if not with_.expressions:
        tree.set("with_", None)


def _grouped(expression: Any, keys: set[str]) -> bool:
    """分组的子查询选出来的一列在每组里只有一个值：整个是分组键，或者每一列都在聚合函数里、或者本身是分组键。"""
    from sqlglot import exp

    if _fingerprint(expression) in keys:
        return True
    if any(isinstance(n, (exp.Window, exp.Query, exp.Subquery)) for n in expression.walk()):
        return False
    for col in expression.find_all(exp.Column):
        if _fingerprint(col) not in keys and col.find_ancestor(exp.AggFunc) is None:
            return False
    return True


def _push_into_groups(tree: Any, read: str | None, info: "_Schema") -> None:
    """（认列之后）外层 WHERE 里只涉及一个分组子查询（FROM 或内连接进来的）选出来的列的条件，换成子查询里对应的表达式、
    挪进它的 HAVING：子查询的每一行就是一组，外层按这一行的值筛和 HAVING 按这一组的值筛，用的是同一个值（分组键取的
    是同一个代表值，聚合是同一个结果）。条件涉及分组键时，分组键得是「相等就是一样」的（见 _Sensitivity._identity_safe）：
    MySQL 会把外层只涉及分组键的条件推进子查询的 WHERE 里先筛行，不分大小写时 BINARY city = 'SH' 推进去和放在 HAVING
    里结果不同（在真实的 MySQL 8.0 上跑出来过）。子查询有窗口函数、取行、去重、ROLLUP 这些的不挪；HAVING 里的条件
    不往 WHERE 挪（相等不等于一样时，组里的行各是各的值）。"""
    from sqlglot import exp

    sens = _Sensitivity(tree, read, info)
    for select in list(tree.find_all(exp.Select)):
        where = select.args.get("where")
        joins = select.args.get("joins") or []
        if where is None or any(str(j.side or "").upper() in ("RIGHT", "FULL") for j in joins):
            continue
        for index, item in enumerate(_items_of(select)):
            if not isinstance(item, exp.Subquery) or (index and not _inner(joins[index - 1])):
                continue
            body = item.this
            if not isinstance(body, exp.Select) or body.args.get("group") is None or sens.of(body) or \
                    any(body.args.get(k) for k in ("limit", "offset", "distinct", "qualify", "windows", "with_")) or \
                    any(n.find(exp.Window) for n in body.expressions):
                continue
            outputs = {p.alias_or_name: p.unalias() for p in body.expressions if not _is_star(p)}
            keys = {_fingerprint(k) for k in body.args["group"].expressions}
            alias = item.alias_or_name
            moved, kept = [], []
            for cond in _conjuncts(where.this):
                cols = list(cond.find_all(exp.Column))
                if not cols or cond.find(exp.Query, exp.Subquery) is not None or \
                        any(c.table != alias or c.name not in outputs or not _grouped(outputs[c.name], keys)
                            for c in cols) or \
                        any(k is not None and not sens._identity_safe(k, body)
                            for c in cols for k in [outputs[c.name] if _fingerprint(outputs[c.name]) in keys else None]):
                    kept.append(cond)
                    continue
                new = cond.copy()
                for col in list(new.find_all(exp.Column)):
                    col.replace(exp.Paren(this=outputs[col.name].copy()))
                moved.append(new)
            if moved:
                having = body.args.get("having")
                body.set("having", exp.Having(this=_and_all(([having.this] if having else []) + moved)))
                select.set("where", exp.Where(this=_and_all(kept)) if kept else None)
                where = select.args.get("where")
                if where is None:
                    break


def _distinct_to_group(tree: Any, sens: "_Sensitivity") -> None:
    """SELECT DISTINCT a, b（没有分组、聚合、窗口函数）和 GROUP BY a, b 出的行一样（空值也算一组）——只在选出来的都是
    「相等就是一样」的列（见 _Sensitivity._identity_safe）、这一段不看行出来的先后（DISTINCT 按扫到的先后出行，GROUP BY
    按分组键排好了出）时改：不然两种写法留下的代表值、取到的行可能不同。ORDER BY 用到没选出来的列时不改（两种写法对
    这种排序的规矩不一样）。已经按选出来的列分组的 DISTINCT 去掉（每组一行、各行的分组键都不同）。"""
    from sqlglot import exp

    for select in list(tree.find_all(exp.Select)):
        distinct = select.args.get("distinct")
        if distinct is None or distinct.args.get("on") or sens.of(select) or _starry(select):
            continue
        group = select.args.get("group")
        if group is not None:
            selected = {_fingerprint(p.unalias()) for p in select.expressions}
            if group.expressions and all(_fingerprint(k) in selected for k in group.expressions) and \
                    not any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets")) and \
                    not any(p.find(exp.Window) for p in select.expressions):
                select.set("distinct", None)
            continue
        if select.args.get("having") is not None or _aggregating(select) or \
                any(p.find(exp.Window, exp.Query, exp.Subquery) for p in select.expressions):
            continue
        keys = [p.unalias() for p in select.expressions]
        if not all(isinstance(k, exp.Column) and sens._identity_safe(k, select) for k in keys):
            continue
        order = select.args.get("order")
        if order is not None:
            allowed = {_fingerprint(k) for k in keys}
            aliases = {p.alias for p in select.expressions if p.alias}
            if any(_fingerprint(o.this) not in allowed and not (
                    isinstance(o.this, exp.Column) and not o.this.table and o.this.name in aliases)
                   for o in order.expressions):
                continue
        new = exp.Group(expressions=[k.copy() for k in keys])
        new.meta[_FROM_DISTINCT] = True
        select.set("distinct", None)
        select.set("group", new)


def _membership_subqueries(tree: Any, sens: "_Sensitivity") -> None:
    """IN / NOT IN (子查询) 只看取值的集合（含不含空值）：里面不取行时，

    - 没有聚合的 ORDER BY 不影响（有聚合的 ORDER BY 让它成了聚合查询，只出一行）；
    - 选出来的只是一列、「相等就是一样」（见 _Sensitivity._identity_safe）时，DISTINCT 不影响；按这一列（和别的列）
      分组、没有 HAVING 也没有聚合时 GROUP BY 不影响（每一行都在某一组里，各组选出来的就是这一列在各行的取值）；
      合并的各支都是这样的一列时，UNION 和 UNION ALL 一样。「相等却不一样」时不改：MySQL 默认不分大小写，去重留下
      'SH' 之后 BINARY 'sh' 就找不到了。"""
    from sqlglot import exp

    def single_column(sub: Any) -> Any:
        if not isinstance(sub, exp.Select) or len(sub.expressions) != 1:
            return None
        column = sub.expressions[0].unalias()
        return column if isinstance(column, exp.Column) and not column.is_star and \
            sens._identity_safe(column, sub) else None

    for node in list(tree.find_all(exp.In)):
        query = node.args.get("query")
        body = query.this if isinstance(query, exp.Subquery) else query
        if body is None:
            continue
        if isinstance(body, exp.SetOperation):
            leaves = _union_leaves(body)
            if leaves is None or any(single_column(leaf) is None for leaf in leaves):
                continue
            _set_union_all(body)
        else:
            leaves = [body]
        for sub in leaves:
            if not isinstance(sub, exp.Select) or any(sub.args.get(k) for k in ("limit", "offset", "fetch", "qualify")):
                continue
            if any(isinstance(n, exp.AggFunc) and not isinstance(n.parent, exp.Window) for n in _block_nodes(sub)) or \
                    sub.args.get("having") is not None:
                continue
            if not any(n.find(exp.Window) for n in sub.expressions):
                sub.set("order", None)
            column = single_column(sub)
            group = sub.args.get("group")
            if group is not None:
                if column is None or any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets", "totals")) or \
                        _fingerprint(column) not in {_fingerprint(k) for k in group.expressions}:
                    continue
                sub.set("group", None)
            distinct = sub.args.get("distinct")
            if distinct is not None and not distinct.args.get("on") and column is not None:
                sub.set("distinct", None)


# ---- 结果看不看行出来的先后

class _Sensitivity:
    """哪几段 SELECT 的结果看行出来的先后：这样的一段不调换来源的先后、不把 CROSS JOIN 写成逗号连接、不排 GROUP BY
    的键、不把 RIGHT JOIN 写成 LEFT JOIN，不往里并子查询——这些改写都会改变库里扫描的先后，实际取到的行、值就不同了。

    本段自己看先后的：取行（LIMIT / OFFSET / FETCH，有 ORDER BY 也可能有并列）；DISTINCT ON；窗口函数；拼串、拼数组、
    取任意一个这类聚合（GROUP_CONCAT）；聚合里既不分组也不聚合的列（SQLite、MySQL 取组里任意一行的值）；当成标量
    用的子查询（SQLite 取第一行），除非是不分组的聚合（只出一行）；去重、分组、MIN / MAX、去重的 UNION 留下的是先
    出现的那个值，而「相等」不等于「写法一样」的时候（MySQL 默认不分大小写的排序规则下的 'SH' 和 'sh'、PostgreSQL
    numeric 的 2.0 和 2.00、SQLite 没有类型亲和的列里的 1 和 1.0、算出来的表达式）看先后，见 _identity_safe；
    PostgreSQL、MySQL 里对不知道是不是精确数的值求和、求平均（浮点数加的先后不同，结果的最后几位就不同，见 _exact）。
    给看先后的一段供行的（它 FROM 里的子查询、引用的 CTE，层层往里）也看先后。"""

    def __init__(self, tree: Any, read: str | None, info: _Schema, resolver: _Resolver | None = None,
                 floats: bool = True) -> None:
        from sqlglot import exp

        self.read = read
        self.info = info
        self.floats = floats
        try:
            self.resolver = resolver or _Resolver(tree)
        except _Unsupported:
            self.resolver = None
        selects = list(tree.find_all(exp.Select))
        marked = {id(s) for s in selects if self._own(s)}
        for op in tree.find_all(exp.SetOperation):
            if op.args.get("distinct") or isinstance(op, (exp.Intersect, exp.Except)):
                for leaf in _leaves(op):
                    if isinstance(leaf, exp.Select) and \
                            not all(self._identity_safe(p.unalias(), leaf) for p in leaf.expressions):
                        marked.add(id(leaf))
        by_id = {id(s): s for s in selects}
        stack = [by_id[i] for i in marked if i in by_id]
        while stack:
            select = stack.pop()
            for item in _items_of(select):
                body = self.resolver.body(item) if self.resolver is not None else (
                    item.this if isinstance(item, exp.Subquery) else None)
                for leaf in _leaves(body) if body is not None else []:
                    if isinstance(leaf, exp.Select) and id(leaf) not in marked:
                        marked.add(id(leaf))
                        stack.append(leaf)
        self.marked = marked

    def of(self, select: Any) -> bool:
        return id(select) in self.marked

    def _own(self, select: Any) -> bool:
        from sqlglot import exp

        if any(select.args.get(k) for k in ("limit", "offset", "fetch")):
            return True
        distinct = select.args.get("distinct")
        if distinct is not None and distinct.args.get("on"):
            return True
        own = list(_block_nodes(select))
        if any(isinstance(n, exp.Window) or _order_dependent(n) for n in own):
            return True
        if _scalar_context(select) and not (_aggregating(select) and select.args.get("group") is None):
            return True
        if _loose_columns(select):
            return True
        if distinct is not None and not all(self._identity_safe(p.unalias(), select) for p in select.expressions):
            return True
        group = select.args.get("group")
        if group is not None and not all(self._identity_safe(k, select) for k in group.expressions):
            return True
        for n in own:
            if isinstance(n, (exp.Min, exp.Max)) and not n.expressions and \
                    not self._identity_safe(n.this.expressions[0] if isinstance(n.this, exp.Distinct) and
                                            n.this.expressions else n.this, select):
                return True
            if self.floats and self.read != "sqlite" and _float_aggregate(n) and not self._exact(n.this, select):
                return True
        return False

    def _exact(self, node: Any, select: Any, depth: int = 0) -> bool:
        """node 的取值是不是精确的数（整数、定点小数）：浮点数求和的先后不同结果就不同（PostgreSQL、MySQL 按扫到的先后
        一个个加；SQLite 用 Kahan-Babuska-Neumaier 求和、关联先后也不跟着 FROM 的先后变，当成精确的）。不知道类型的不算。"""
        from sqlglot import exp

        node = _unparen(node)
        if isinstance(node, exp.Distinct) and len(node.expressions) == 1:
            node = node.expressions[0]
        if isinstance(node, exp.Count) or (isinstance(node, exp.Literal) and node.is_int):
            return True
        if isinstance(node, (exp.Add, exp.Sub, exp.Mul)):
            return self._exact(node.left, select, depth) and self._exact(node.right, select, depth)
        if not isinstance(node, exp.Column) or node.is_star or depth > 16 or self.resolver is None:
            return False
        item = self.resolver.source_of.get(id(node))
        if item is None:
            items = _items_of(select)
            item = items[0] if len(items) == 1 and not node.table else None
        if item is None:
            return False
        body = self.resolver.body(item)
        if body is None:
            columns = self.info.types.get(item.name.translate(_ASCII_LOWER)) if isinstance(item, exp.Table) else None
            declared = (columns or {}).get(node.name.translate(_ASCII_LOWER), "").lower()
            return any(declared.startswith(w) for w in ("int", "integer", "bigint", "smallint", "tinyint", "mediumint",
                                                        "serial", "bigserial", "decimal", "numeric", "dec"))
        if isinstance(body, exp.Select):
            for p in body.expressions:
                if not _is_star(p) and p.alias_or_name == node.name:
                    return self._exact(p.unalias(), body, depth + 1)
        return False

    def _identity_safe(self, node: Any, select: Any, depth: int = 0) -> bool:
        """node 的取值里「相等」是不是就是「一样」：COUNT 是整数；光一列的话看它是哪张表的哪一列、声明的什么类型。"""
        from sqlglot import exp

        node = _unparen(node)
        if isinstance(node, exp.Count):
            return True
        if not isinstance(node, exp.Column) or node.is_star or depth > 16:
            return False
        item = self.resolver.source_of.get(id(node)) if self.resolver is not None else None
        if item is None:
            items = _items_of(select)
            item = items[0] if len(items) == 1 and not node.table else None
        if item is None:
            return self._safe_type(None)
        body = self.resolver.body(item) if self.resolver is not None else (
            item.this if isinstance(item, exp.Subquery) else None)
        if body is None:
            if not isinstance(item, exp.Table):
                return False
            columns = self.info.types.get(item.name.translate(_ASCII_LOWER))
            declared = columns.get(node.name.translate(_ASCII_LOWER)) if columns is not None else None
            return self._safe_type(declared)
        if not isinstance(body, exp.Select):
            return False
        for p in body.expressions:
            if not _is_star(p) and p.alias_or_name == node.name:
                return self._identity_safe(p.unalias(), body, depth + 1)
        sources = _items_of(body)
        if any(isinstance(p, exp.Star) for p in body.expressions) and len(sources) == 1:
            return self._through_star(sources[0], node.name, depth + 1)
        return False

    def _through_star(self, item: Any, name: str, depth: int) -> bool:
        """SELECT * 原样透出来的一列：到它唯一的来源里找同名的列。"""
        from sqlglot import exp

        body = self.resolver.body(item) if self.resolver is not None else None
        if body is None:
            if not isinstance(item, exp.Table):
                return False
            columns = self.info.types.get(item.name.translate(_ASCII_LOWER))
            return self._safe_type(columns.get(name.translate(_ASCII_LOWER)) if columns is not None else None)
        if not isinstance(body, exp.Select) or depth > 16:
            return False
        for p in body.expressions:
            if not _is_star(p) and p.alias_or_name == name:
                return self._identity_safe(p.unalias(), body, depth + 1)
        sources = _items_of(body)
        if any(isinstance(p, exp.Star) for p in body.expressions) and len(sources) == 1:
            return self._through_star(sources[0], name, depth + 1)
        return False

    def _safe_type(self, declared: str | None) -> bool:
        """声明的类型下「相等」是不是就是「一样」。SQLite 没给表结构时按列有声明的类型、默认的排序规则算（假设）；
        有类型的话 BLOB、没写类型（没有类型亲和）的列可能混着 1 和 1.0。PostgreSQL、MySQL 不知道类型就不算：numeric
        的 2.0 和 2.00、MySQL 默认不分大小写的字符串都是相等写法却不一样。"""
        if self.read == "sqlite":
            if declared is None:
                return True
            upper = declared.upper()
            if "INT" in upper or any(w in upper for w in ("CHAR", "CLOB", "TEXT", "REAL", "FLOA", "DOUB")):
                return True
            return bool(upper) and "BLOB" not in upper
        if declared is None:
            return False
        lower = declared.lower().strip()
        if self.read == "mysql":
            return any(lower.startswith(w) for w in ("int", "integer", "bigint", "smallint", "tinyint", "mediumint",
                                                     "date", "datetime", "timestamp", "time", "year", "decimal",
                                                     "numeric", "bit", "bool"))
        return any(lower.startswith(w) for w in ("int", "integer", "bigint", "smallint", "serial", "bigserial",
                                                 "text", "varchar", "character", "char", "bpchar", "date",
                                                 "timestamp", "time", "bool", "uuid"))


def _float_aggregate(node: Any) -> bool:
    """浮点数求和类的聚合（不在窗口函数里的）。"""
    from sqlglot import exp

    if isinstance(node.parent, exp.Window):
        return False
    name = type(node).__name__.lower()
    if isinstance(node, exp.Anonymous):
        name = str(node.name).lower().replace("_", "")
    return name in _FLOAT_AGGS


def _order_dependent(node: Any) -> bool:
    from sqlglot import exp

    name = type(node).__name__.lower()
    if isinstance(node, exp.Anonymous):
        name = str(node.name).lower().replace("_", "")
    return name in _ORDERED_AGGS


def _aggregating(select: Any) -> bool:
    """这一段是聚合查询：有 GROUP BY，或者自己的 SELECT 列表、HAVING、ORDER BY 里有聚合函数（不算窗口函数）。"""
    from sqlglot import exp

    if select.args.get("group") is not None or select.args.get("having") is not None:
        return True
    return any(isinstance(n, exp.AggFunc) and not isinstance(n.parent, exp.Window) for n in _block_nodes(select)
               if not isinstance(n.parent, exp.Window))


def _scalar_context(query: Any) -> bool:
    """这段查询当成一个值用（标量子查询）：不在 FROM / JOIN、IN、EXISTS、ANY / ALL、CTE 里，也不是整条语句。"""
    from sqlglot import exp

    node = query
    while True:
        parent = node.parent
        if parent is None:
            return False
        if isinstance(parent, exp.Subquery):
            node = parent
            continue
        if isinstance(parent, exp.SetOperation):
            node = parent
            continue
        if isinstance(parent, (exp.From, exp.Join, exp.Exists, exp.CTE, exp.Any, exp.All)):
            return False
        if isinstance(parent, exp.In) and node.arg_key == "query":
            return False
        return True


def _loose_columns(select: Any) -> bool:
    """聚合的一段里既不在聚合函数里、也不是分组键（的一部分）的列：SQLite、MySQL 取组里任意一行的值。"""
    from sqlglot import exp

    if not _aggregating(select):
        return False
    group = select.args.get("group")
    keys = {_fingerprint(k) for k in (group.expressions if group is not None else [])}

    def loose(node: Any) -> bool:
        if isinstance(node, (exp.Query, exp.Subquery)) or _fingerprint(node) in keys:
            return False
        if isinstance(node, exp.AggFunc) and not isinstance(node.parent, exp.Window):
            return False
        if isinstance(node, exp.Window):
            return False
        if isinstance(node, exp.Column):
            return not node.is_star
        return any(loose(c) for c in node.iter_expressions())

    clauses = [c for c in (*select.expressions, select.args.get("having"), select.args.get("order")) if c is not None]
    return any(loose(c) for c in clauses)


# ---- 动来源的改写

def _unwrap_passthrough(tree: Any, read: str | None) -> Any:
    """原样选出 FROM 里唯一一个子查询的全部列、什么别的都不做的一段（SELECT * FROM (…) t、SELECT t.a, t.b FROM (…) t），
    换成那个子查询：出的行一样，列的先后也一样。最外层和条件里的子查询列名不影响取值，列名可以不同；别的地方（FROM 里
    的子查询、CTE）外面按名字引用，名字也得一样。不换的：子查询自己带 ORDER BY / LIMIT / OFFSET（套一层之后顺序、取行
    不一定照旧）；子查询是 UNION 这类合并、库是 SQLite（和不知道是哪种库时）——SQLite 从子查询里读合并的结果时，按第一支
    那一列的类型亲和转换别的支的值（REAL 列在前，另一支的整数 2 读出来成了 2.0），套不套这一层结果不一样。"""
    from sqlglot import exp

    def outputs(query: Any) -> list[str] | None:
        if any(query.args.get(k) is not None for k in ("order", "limit", "offset", "fetch")):
            return None
        if isinstance(query, exp.SetOperation):
            if read in (None, "sqlite"):
                return None
            leaves = _leaves(query)
            return outputs(leaves[0]) if leaves and isinstance(leaves[0], exp.Select) else None
        if not isinstance(query, exp.Select) or _starry(query) or query.args.get("with_") is not None:
            return None
        names = [p.alias_or_name for p in query.expressions]
        return names if all(names) and len(set(names)) == len(names) else None

    changed = True
    while changed:
        changed = False
        for select in list(tree.find_all(exp.Select)):
            if select.args.get("joins") or select.args.get("from_") is None or \
                    {k for k, v in select.args.items() if v} - {"expressions", "from_", "with_"} or \
                    (select.args.get("with_") is not None and select is not tree):
                continue
            item = select.args["from_"].this
            if not isinstance(item, exp.Subquery):
                continue
            body = item.this
            names = outputs(body)
            if names is None:
                continue
            alias = item.alias_or_name
            star = len(select.expressions) == 1 and isinstance(select.expressions[0], exp.Star)
            cols = [p.unalias() for p in select.expressions]
            if not star and (len(cols) != len(names) or any(
                    not isinstance(c, exp.Column) or c.is_star or c.name != n or (c.table and c.table != alias)
                    for c, n in zip(cols, names))):
                continue
            renamed = not star and any(p.alias_or_name != n for p, n in zip(select.expressions, names))
            parent = select.parent
            loose = select is tree or isinstance(parent, (exp.In, exp.Exists)) or (
                isinstance(parent, exp.Subquery) and not isinstance(parent.parent, (exp.From, exp.Join, exp.CTE)))
            if renamed and not loose:
                continue
            new = body.copy()
            if select is tree:
                if select.args.get("with_") is not None:
                    if new.args.get("with_") is not None:
                        continue
                    new.set("with_", select.args["with_"])
                tree = new
            else:
                select.replace(new)
            changed = True
            break
    return tree


def _bare_star(select: Any) -> bool:
    from sqlglot import exp

    return any(isinstance(p, exp.Star) for p in select.expressions)


def _right_to_left(tree: Any, sens: _Sensitivity) -> None:
    """只有两个来源的 a RIGHT JOIN b ON p 就是 b LEFT JOIN a ON p。选的是 * 时列的先后跟着变、结果看行出来的先后的
    一段扫描的先后跟着变，不改。"""
    from sqlglot import exp

    for select in list(tree.find_all(exp.Select)):
        joins = select.args.get("joins") or []
        if len(joins) != 1 or str(joins[0].side or "").upper() != "RIGHT" or not _plain_join(joins[0]) or \
                joins[0].args.get("on") is None or _bare_star(select) or sens.of(select):
            continue
        first = select.args["from_"]
        left, right = first.this, joins[0].this
        first.set("this", right.copy())
        joins[0].set("this", left.copy())
        joins[0].set("side", "LEFT")
        joins[0].set("kind", None)
        joins[0].meta[_FLIPPED] = True
    for join in tree.find_all(exp.Join):
        if join.side and str(join.kind or "").upper() == "OUTER":
            join.set("kind", None)


def _quantified(node: Any) -> bool:
    """ANY / SOME / ALL (子查询或数组)：sqlglot 有时解析成 Any / All 节点，有时（ALL (ARRAY[]::int[])）解析成名字叫
    ALL 的函数调用。拿它比较的结果不是普通比较的规矩：x > ALL (空集) 是真，NOT (x = ANY …) 也不是 x <> ANY …。"""
    from sqlglot import exp

    node = _unparen(node)
    return isinstance(node, (exp.Any, exp.All)) or (
        isinstance(node, exp.Anonymous) and str(node.name).upper() in ("ALL", "ANY", "SOME"))


def _null_propagating(node: Any, alias: Any, read: str | None) -> bool:
    """alias 那一侧整行是空值时，node 一定是空值：那一侧的列，或者对它做算术、取负、取大小写、取长度、绝对值、转类型；
    SQLite 里的 || 拼接（拿空值拼得空值）。PostgreSQL 的 || 不算（空的数组拼上一个数组还是那个数组：NULL || ARRAY['x']
    是 ARRAY['x']），MySQL 的 || 默认是 OR，CONCAT 在 PostgreSQL 里跳过空值：都不算。alias 也可以是判断「这一列是不是
    那个为空的东西」的函数（见 _drop_implied_not_null：按一列为空算）。"""
    from sqlglot import exp

    node = _unparen(node)
    if isinstance(node, exp.Column):
        if callable(alias):
            return bool(alias(node))
        return bool(node.table) and node.table == alias
    if type(node).__name__ in _STRICT_UNARY:
        return _null_propagating(node.this, alias, read)
    if type(node).__name__ in _STRICT_BINARY or (isinstance(node, exp.DPipe) and read == "sqlite"):
        return _null_propagating(node.left, alias, read) or _null_propagating(node.right, alias, read)
    return False


#: 拿空值比较得空值的比较（ANY / ALL 另算，见 _quantified）
_COMPARISON_NODES = ("EQ", "NEQ", "GT", "GTE", "LT", "LTE", "Like", "ILike")


def _strict(cond: Any, alias: Any, read: str | None) -> bool:
    """alias 那一侧整行是空值时，cond 一定是空值（不是真、也不是假）：一边一定是空值的比较、LIKE、BETWEEN，非空常量
    列表的 IN，以及这些用 NOT、AND、OR 连起来（NOT 空值还是空值，空值 AND / OR 空值还是空值）。
    不算的：IN 子查询（子查询没有行时是假，不是空值）、x IN ()（SQLite 允许空列表，恒为假）、ANY / ALL（子查询没有行
    时是假或真）、IS [NOT] NULL、COALESCE、CASE。"""
    from sqlglot import exp

    cond = _unparen(cond)
    if type(cond).__name__ in _COMPARISON_NODES:
        if _quantified(cond.left) or _quantified(cond.right):
            return False
        return _null_propagating(cond.left, alias, read) or _null_propagating(cond.right, alias, read)
    if isinstance(cond, exp.In):
        if cond.args.get("query") or cond.args.get("unnest") or cond.args.get("field") or not cond.expressions:
            return False
        return _null_propagating(cond.this, alias, read)
    if isinstance(cond, exp.Between):
        return _null_propagating(cond.this, alias, read)
    if isinstance(cond, exp.Not):
        return _strict(cond.this, alias, read)
    if isinstance(cond, (exp.And, exp.Or)):
        return _strict(cond.left, alias, read) and _strict(cond.right, alias, read)
    return False


def _rejects(cond: Any, alias: Any, read: str | None) -> bool:
    """alias 那一侧整行是空值时 cond 一定不成立（是空值或假），WHERE 里有它就留不下没关联上的行：

    - 一定是空值的（见 _strict）；
    - x IS NOT NULL；x IN (子查询)、x op ANY (子查询)（子查询没有行时是假，有行时 x 为空是空值）；x IN ()（恒为假）；
      BETWEEN 的上下界一定是空值的（x >= 空值 AND x <= b 不会是真）；
    - NOT 只能套一定是空值的（NOT 假是真：NOT (x IN (空集)) 在 x 为空时是真，NOT IN (空集) 留得下没关联上的行）；
    - AND 里有一项不成立、OR 里每一项都不成立。

    x op ALL (…) 不算（子查询没有行时是真）；IS NULL、COALESCE、CASE、<=>、IS DISTINCT FROM 之类不算。"""
    from sqlglot import exp

    cond = _unparen(cond)
    if _strict(cond, alias, read):
        return True
    if type(cond).__name__ in _COMPARISON_NODES:
        for side, other in ((cond.left, cond.right), (cond.right, cond.left)):
            if isinstance(_unparen(other), exp.Any) and _null_propagating(side, alias, read):
                return True
        return False
    if isinstance(cond, exp.In):
        if cond.args.get("unnest") or cond.args.get("field"):
            return False
        if not cond.expressions and not cond.args.get("query"):
            return True
        return bool(cond.args.get("query")) and _null_propagating(cond.this, alias, read)
    if isinstance(cond, exp.Between):
        return any(_null_propagating(cond.args.get(k), alias, read) for k in ("low", "high") if cond.args.get(k))
    if isinstance(cond, exp.Not):
        inner = _unparen(cond.this)
        if isinstance(inner, exp.Is) and isinstance(_unparen(inner.expression), exp.Null):
            return _null_propagating(inner.this, alias, read)
        return _strict(inner, alias, read)
    if isinstance(cond, exp.And):
        return _rejects(cond.left, alias, read) or _rejects(cond.right, alias, read)
    if isinstance(cond, exp.Or):
        return _rejects(cond.left, alias, read) and _rejects(cond.right, alias, read)
    return False


def _reject_nulls(tree: Any, read: str | None, sens: "_Sensitivity") -> None:
    """LEFT JOIN 关联进来的那一侧，同一段 WHERE 里有把它整行为空的行筛掉的条件（见 _rejects）：没关联上的行反正留
    不下来，这次外连接就是内连接。有 RIGHT / FULL JOIN 的段不动；结果看行出来先后的一段（GROUP_CONCAT、有并列的取行，
    见 _Sensitivity）也不动：SQLite 自己把这样的 LEFT JOIN 降成内连接之后，扫描的先后和直接写内连接的不一样（真实的
    SQLite 上 EXPLAIN QUERY PLAN 一个先扫 refunds、一个先扫 orders，GROUP_CONCAT 拼出来的先后就不同，第 3 轮自己压测
    时找到的）。"""
    from sqlglot import exp

    for select in list(tree.find_all(exp.Select)):
        joins = select.args.get("joins") or []
        where = select.args.get("where")
        if where is None or any(str(j.side or "").upper() in ("RIGHT", "FULL") for j in joins) or sens.of(select):
            continue
        conds = _conjuncts(where.this)
        for join in joins:
            if str(join.side or "").upper() != "LEFT" or not _plain_join(join):
                continue
            alias = join.this.alias_or_name
            if alias and any(_rejects(c, alias, read) for c in conds):
                join.set("side", None)
                join.set("kind", None)
                join.this.meta[_REJECTED] = "RIGHT JOIN" if join.meta.get(_FLIPPED) else "LEFT JOIN"


#: 跳过空值的聚合：参数在一行里是空值时这一行不算数（COUNT(*) 数的是行数，不算）
_NULL_SKIPPING = ("Sum", "Avg", "Min", "Max", "Count")


def _aggregate_args(select: Any) -> list[Any] | None:
    """这一段 SELECT 列表、HAVING、ORDER BY 里的聚合（不算窗口函数）都是跳过空值的那几种时，返回它们的参数（DISTINCT
    拆开）；有别的聚合、COUNT(*)、多个参数的、套着子查询或窗口函数的返回 None。"""
    from sqlglot import exp

    clauses = [c for c in (*select.expressions, select.args.get("having"), select.args.get("order")) if c is not None]
    if any(c.find(exp.Query, exp.Subquery, exp.Window) is not None for c in clauses):
        return None
    args = []
    for clause in clauses:
        for agg in clause.find_all(exp.AggFunc):
            if type(agg).__name__ not in _NULL_SKIPPING or agg.args.get("expressions"):
                return None
            arg = agg.this
            if isinstance(arg, exp.Distinct):
                if len(arg.expressions) != 1:
                    return None
                arg = arg.expressions[0]
            if arg is None or isinstance(arg, exp.Star):
                return None
            args.append(arg)
    return args


def _null_blind_left_joins(tree: Any, read: str | None, sens: _Sensitivity) -> None:
    """不分组、结果只是聚合的一段（只出一行），所有聚合都跳过空值（SUM、AVG、MIN、MAX、COUNT(x)，可以去重）、参数在
    LEFT JOIN 可空一侧整行为空时一定是空值（见 _null_propagating）：这次 LEFT JOIN 写成内连接结果一样。LEFT JOIN 的
    结果是内连接的结果加上没关联上的行（那一侧整行是空值），之后的内连接、LEFT JOIN、WHERE 都是一行一行处理的，没
    关联上的行带来的那些行里这一侧也都是空值，在这些聚合里都被跳过；COALESCE(SUM(r.amount), 0)、SUM(x) / COUNT(y)、
    旁边多一个常量列都只是拿这几个聚合再算。不改的：分组（没关联上的行自己成一组）、COUNT(*)、参数可能不为空的
    （COALESCE(r.amount, 0)）、聚合以外还有列、有 RIGHT / FULL JOIN、结果看行出来先后的一段（PostgreSQL / MySQL 里
    浮点数求和的先后会变，见 _Sensitivity）。"""
    from sqlglot import exp

    for select in list(tree.find_all(exp.Select)):
        joins = select.args.get("joins") or []
        if not joins or any(str(j.side or "").upper() in ("RIGHT", "FULL") for j in joins):
            continue
        group = select.args.get("group")
        if (group is not None and group.expressions) or sens.of(select) or _loose_columns(select):
            continue
        args = _aggregate_args(select)
        if not args:
            continue
        for join in joins:
            if str(join.side or "").upper() != "LEFT" or not _plain_join(join) or join.args.get("on") is None:
                continue
            alias = join.this.alias_or_name
            if alias and all(_null_propagating(arg, alias, read) for arg in args):
                join.set("side", None)
                join.set("kind", None)
                join.this.meta[_NULL_BLIND] = True


def _membership_context(select: Any) -> bool:
    """这一段（或者它所在的 UNION 的一支）是 IN 子查询、EXISTS 子查询的全部：重复的行不影响结果。"""
    from sqlglot import exp

    node = select
    while isinstance(node.parent, (exp.Subquery, exp.Union)):
        if isinstance(node.parent, exp.Subquery) and node.parent.alias:
            return False
        node = node.parent
    parent = node.parent
    return isinstance(parent, exp.Exists) or (isinstance(parent, exp.In) and node.arg_key == "query")


def _dup_insensitive(select: Any) -> bool:
    """这一段的结果不怕来源的行重复：聚合查询里的聚合都是 MIN、MAX、COUNT / SUM / AVG(DISTINCT …) 这类去重的；不聚合
    时 SELECT DISTINCT（不是 DISTINCT ON），或者整段是 IN / EXISTS 子查询。有窗口函数的不算。"""
    from sqlglot import exp

    own = list(_block_nodes(select))
    if any(isinstance(n, exp.Window) for n in own):
        return False
    distinct = select.args.get("distinct")
    if distinct is not None and distinct.args.get("on"):
        return False
    if _aggregating(select):
        aggs = [n for n in own if isinstance(n, exp.AggFunc) and not isinstance(n.parent, exp.Window)]
        return all((isinstance(a, (exp.Min, exp.Max)) and not a.args.get("expressions")) or
                   (type(a).__name__ in ("Count", "Sum", "Avg") and isinstance(a.this, exp.Distinct) and
                    len(a.this.expressions) == 1) for a in aggs)
    return distinct is not None or _membership_context(select)


def _drop_unused_left_joins(tree: Any, sens: _Sensitivity) -> None:
    """LEFT JOIN 进来的表除了它自己的 ON 之外哪儿都没用到（本段、相关子查询、后面的连接都不引用它），而且结果不怕重复
    的行（SELECT DISTINCT；聚合都是 MIN、MAX、COUNT(DISTINCT …) 这类去重的；整段是 IN / EXISTS 子查询）：这次 LEFT
    JOIN 去掉结果一样——LEFT JOIN 不会让左边的行变少，只会让关联上好几行的左边的行重复出现，别的列的取值不变。不去的：
    有 COUNT(*)、SUM 这类怕重复的聚合（表结构里说 ON 是按唯一键关联的也不去：类型亲和、COLLATE、部分唯一索引、MySQL
    的隐式转换都能让一行关联上好几行，见 _finish），有 RIGHT / FULL JOIN、窗口函数、SELECT *，有不带表名的列（分不清
    是不是这张表的），结果看行出来先后的一段（DISTINCT 留下先扫到的那个值、取行，见 _Sensitivity）。"""
    from sqlglot import exp

    changed = True
    while changed:
        changed = False
        for select in list(tree.find_all(exp.Select)):
            joins = select.args.get("joins") or []
            if not joins or any(str(j.side or "").upper() in ("RIGHT", "FULL") for j in joins) or sens.of(select) or \
                    any(isinstance(p, exp.Star) for p in select.expressions):
                continue
            insensitive = _dup_insensitive(select)
            own = list(_block_nodes(select))
            nested = [c for sub in _nested_queries(select) for c in sub.find_all(exp.Column)]
            columns = [n for n in own if isinstance(n, exp.Column)] + nested
            if any(not c.table for c in columns):
                continue
            for join in joins:
                if str(join.side or "").upper() != "LEFT" or not _plain_join(join) or join.args.get("on") is None:
                    continue
                alias = join.this.alias_or_name
                on = {id(n) for n in join.args["on"].walk()}
                if alias and insensitive and not any(c.table == alias and id(c) not in on for c in columns):
                    select.set("joins", [j for j in joins if j is not join] or None)
                    dropped = select.meta.setdefault(_DROPPED_JOINS, [])
                    dropped += [t.name for t in join.this.find_all(exp.Table)] or [alias]
                    changed = True
                    break
            if changed:
                break


def _drop_implied_not_null(tree: Any, read: str | None, sens: _Sensitivity) -> None:
    """WHERE、HAVING、ON 里 AND 连着的 x IS NOT NULL，同一组 AND 里另有一项在 x 为空时一定不成立（x = y、x > 3、
    x IN (…)，见 _rejects）时去掉：那一项成立时 x 已经不为空（这几处只认「是真」，假和空值一样筛掉）。表结构里说 x
    不为空（NOT NULL、主键）的不去：约束不拿来改写（见 _finish）。

    只认带表名的一列；外连接改内连接（_reject_nulls）在这之前做完，靠 IS NOT NULL 改成的内连接不受影响。"""
    from sqlglot import exp

    def target(part: Any) -> Any:
        part = _unparen(part)
        if isinstance(part, exp.Not):
            inner = _unparen(part.this)
            if isinstance(inner, exp.Is) and isinstance(_unparen(inner.expression), exp.Null) and \
                    isinstance(_unparen(inner.this), exp.Column) and _unparen(inner.this).table:
                return _unparen(inner.this)
        return None

    for select in list(tree.find_all(exp.Select)):
        if sens.of(select):
            # 少一个条件可能换一种扫描的先后（走不走索引），结果看行出来先后的一段不动
            continue
        joins = select.args.get("joins") or []
        holders = [(select.args.get("where"), "this"), (select.args.get("having"), "this")] + \
            [(j, "on") for j in joins if j.args.get("on") is not None]
        for holder, key in holders:
            if holder is None:
                continue
            parts = _conjuncts(holder.args.get(key))
            kept: list[Any] = []
            for i, part in enumerate(parts):
                column = target(part)
                if column is not None:
                    fp = _fingerprint(column)
                    others = kept + parts[i + 1:]
                    if any(_rejects(o, lambda c, fp=fp: _fingerprint(c) == fp, read) for o in others):
                        continue
                kept.append(part)
            if len(kept) == len(parts):
                continue
            if kept:
                holder.set(key, _and_all([p.copy() for p in kept]))
            elif isinstance(holder, exp.Join):
                holder.set(key, exp.true())
            elif isinstance(holder, exp.Having) and not _grouped_select(select):
                holder.set(key, exp.true())
            else:
                select.set(holder.arg_key, None)


#: 换成同一组相等的列之后，原文写的那一列（实表名、列名）记在新列的 meta 里：比等价时不算（见 _fingerprint），差异
#: 说明给人看时照原文写（见 _Explainer.show）
_WRITTEN_COLUMN = "sql_semantics_written_column"


def _substitute_equal_columns(tree: Any, read: str | None, info: _Schema, sens: _Sensitivity) -> None:
    """（认列之后）WHERE 里 AND 连着的「两列相等」（内连接的条件挪进 WHERE 之后也在这里）对 WHERE 之后的每一行都成立：
    两列都不为空、而且相等。两列声明的类型完全一样、这个类型下「相等就是一样」（整数、文本这类，见 _identity_type）时，
    SELECT 列表、GROUP BY、HAVING、ORDER BY 里用哪一列都是同一个值：都换成同一组里排在最前的那一列（按表名、列名排；
    同一张表关联了两次、分不清该选哪个别名的不换）。这样「按 users.id 分组」和「按 orders.user_id 分组」（ON
    o.user_id = u.id）就认得出是一回事。不换的：没给表结构、类型不一样（SQLite 里 1 和 '1' 也相等）、LEFT JOIN 的
    ON 里的相等（没关联上的行一边是空值）、本段有既不分组也不聚合的列（PostgreSQL 里那是错的写法，换了反而成了对的）。"""
    from sqlglot import exp

    for select in list(tree.find_all(exp.Select)):
        where = select.args.get("where")
        if where is None or _loose_columns(select) or sens.of(select):
            continue
        items = {i.alias_or_name: i for i in _items_of(select)}
        ctes = _visible_ctes(select)

        def base(col: Any) -> tuple[str, str, str] | None:
            item = items.get(col.table)
            if not isinstance(item, exp.Table) or item.args.get("db") or \
                    item.name.translate(_ASCII_LOWER) in ctes:
                return None
            table, column = item.name.translate(_ASCII_LOWER), col.name.translate(_ASCII_LOWER)
            declared = info.types.get(table, {}).get(column)
            if not declared or not _identity_type(declared, read, sens):
                return None
            return table, column, declared.strip().upper()

        parent: dict[tuple[str, str], tuple[str, str]] = {}

        def find(x: tuple[str, str]) -> tuple[str, str]:
            parent.setdefault(x, x)
            while parent[x] != x:
                x = parent[x]
            return x

        bases: dict[tuple[str, str], tuple[str, str, str]] = {}
        for cond in _conjuncts(where.this):
            cond = _unparen(cond)
            if not isinstance(cond, exp.EQ):
                continue
            left, right = _unparen(cond.this), _unparen(cond.expression)
            if not (isinstance(left, exp.Column) and isinstance(right, exp.Column) and left.table and right.table):
                continue
            bl, br = base(left), base(right)
            if bl is None or br is None or bl[2] != br[2]:
                continue
            kl, kr = (left.table, left.name), (right.table, right.name)
            bases[kl], bases[kr] = bl, br
            parent[find(kl)] = find(kr)
        classes: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for member in list(parent):
            classes.setdefault(find(member), []).append(member)
        replace: dict[tuple[str, str], tuple[str, str]] = {}
        for members in classes.values():
            if len(members) < 2:
                continue
            ranked = sorted(members, key=lambda m: bases[m][:2])
            if len({bases[m][:2] for m in members}) != len(members):
                continue
            for m in members:
                if m != ranked[0]:
                    replace[m] = ranked[0]
        if not replace:
            continue
        clauses = [*select.expressions, select.args.get("group"), select.args.get("having"), select.args.get("order")]
        for clause in [c for c in clauses if c is not None]:
            for col in list(clause.find_all(exp.Column)):
                if col.find_ancestor(exp.Select) is not select or (col.table, col.name) not in replace:
                    continue
                table, name = replace[(col.table, col.name)]
                new = exp.column(name, table=table)
                new.meta[_WRITTEN_COLUMN] = bases[(col.table, col.name)][:2]
                if col.parent is select and col.arg_key == "expressions":
                    new = exp.alias_(new, col.alias_or_name, copy=False)
                col.replace(new)


def _identity_type(declared: str, read: str | None, sens: _Sensitivity | None) -> bool:
    """声明的类型下「相等就是一样」、可以拿同一组相等的列互换：SQLite 里整数、文本亲和的列（实数亲和的 0.0 和 -0.0
    相等却不一样，数值亲和、没有亲和的列能同时存 1 和 1.0）；PostgreSQL、MySQL 按 _Sensitivity._safe_type。"""
    upper = declared.upper()
    if read == "sqlite":
        return "INT" in upper or any(w in upper for w in ("CHAR", "CLOB", "TEXT"))
    return sens is not None and sens._safe_type(declared)


def _int_value(node: Any) -> int | None:
    """只由整数常量和加、减、乘、取负组成的式子的值（LIMIT 1 + 2 是 3）；别的返回 None。"""
    from sqlglot import exp

    node = _unparen(node)
    if isinstance(node, exp.Literal) and node.is_int:
        return int(node.this)
    if isinstance(node, exp.Neg):
        value = _int_value(node.this)
        return None if value is None else -value
    if isinstance(node, (exp.Add, exp.Sub, exp.Mul)):
        left, right = _int_value(node.left), _int_value(node.right)
        if left is None or right is None:
            return None
        if isinstance(node, exp.Add):
            return left + right
        return left - right if isinstance(node, exp.Sub) else left * right
    return None


def _normalize_limits(tree: Any, read: str | None) -> None:
    """取行的几种同义写法：

    - SQLite、PostgreSQL 的 LIMIT / OFFSET 可以写算式：只由整数常量加减乘出来的算成一个数（LIMIT 1 + 2 就是 LIMIT 3）。
      MySQL 的 LIMIT 只能写常量，算式是语法错，不算。
    - SQLite 的 LIMIT 是负数就是不限行数（SQLite 文档原话）：没有 OFFSET 时去掉 LIMIT，有 OFFSET 时统一写成 LIMIT -1。
      PostgreSQL 的负数 LIMIT 报错，不动。
    - PostgreSQL 的 LIMIT NULL 和 LIMIT ALL（sqlglot 解析时已经去掉）就是不限行数（PostgreSQL 文档原话）。SQLite 的
      LIMIT NULL 报错，不动。"""
    from sqlglot import exp

    def number(value: int) -> Any:
        return exp.Literal.number(value) if value >= 0 else exp.Neg(this=exp.Literal.number(-value))

    if read not in ("sqlite", "postgres"):
        return
    for query in list(tree.find_all(exp.Select, exp.SetOperation)):
        for key in ("limit", "offset"):
            node = query.args.get(key)
            if not isinstance(node, (exp.Limit, exp.Offset)) or node.expression is None:
                continue
            value = _int_value(node.expression)
            if value is not None:
                node.set("expression", number(value))
        limit = query.args.get("limit")
        if not isinstance(limit, exp.Limit) or limit.expression is None:
            continue
        if isinstance(query, exp.Select) and (_loose_columns(query) or any(
                isinstance(n, exp.Window) or _order_dependent(n) for n in _block_nodes(query))):
            # 有 GROUP_CONCAT、窗口函数、既不分组也不聚合的列：去不去掉 LIMIT 子句可能换一种扫描的先后，结果跟着变
            continue
        value = _int_value(limit.expression)
        if read == "sqlite" and value is not None and value < 0:
            if query.args.get("offset") is None:
                query.set("limit", None)
            else:
                limit.set("expression", number(-1))
        elif read == "postgres" and isinstance(_unparen(limit.expression), exp.Null):
            query.set("limit", None)


def _dedupe_order(tree: Any, sens: _Sensitivity) -> None:
    """ORDER BY 里和前面某一项一模一样的项去掉（不管升序降序）：前面那项相等的行，这一项也相等，排不出先后。结果看行
    出来先后的一段（取行、窗口函数、GROUP_CONCAT）不去：并列的行谁在前要看排序的实现，多一个排序键可能就换了。"""
    from sqlglot import exp

    for order in list(tree.find_all(exp.Order)):
        owner = order.find_ancestor(exp.Select)
        if owner is None or sens.of(owner):
            continue
        seen: set[str] = set()
        kept = []
        for item in order.expressions:
            key = _fingerprint(item.this if isinstance(item, exp.Ordered) else item)
            if key not in seen:
                seen.add(key)
                kept.append(item)
        if len(kept) != len(order.expressions):
            order.set("expressions", kept)


def _flatten_prefix(tree: Any, sens: _Sensitivity, read: str | None) -> None:
    """开头那组内连接（FROM 和紧跟着的内连接、CROSS JOIN、逗号连接，到第一个外连接为止）的 ON 条件挪进 WHERE、写成
    逗号连接：内连接的条件写在 ON 还是 WHERE 一样；后面的 LEFT JOIN 对左边每一行各自关联，左边先筛后筛一样。后面有
    RIGHT / FULL JOIN 的不动：没关联上的那一侧整行空值，挪到 WHERE 的条件会把它们筛掉（PostgreSQL、MySQL 里逗号还比
    JOIN 结合得松，a, b RIGHT JOIN c 是 a 乘上 b RIGHT JOIN c）。内连接的条件写在 ON 还是 WHERE，优化器一视同仁，结果
    看行出来先后的一段也照做；只有 SQLite 里结果看先后、又写了 CROSS JOIN 的一段不动（SQLite 不调换 CROSS JOIN 两边、
    会调换逗号连接）。"""
    from sqlglot import exp

    for select in list(tree.find_all(exp.Select)):
        joins = select.args.get("joins") or []
        if not joins or select.args.get("from_") is None or \
                any(str(j.side or "").upper() in ("RIGHT", "FULL") for j in joins):
            continue
        if read in (None, "sqlite") and sens.of(select) and \
                any(str(j.kind or "").upper() == "CROSS" or j.meta.get("sql_semantics_join") for j in joins):
            continue
        moved: list[Any] = []
        for join in joins:
            if not _inner(join):
                break
            on = join.args.get("on")
            if on is not None:
                moved += _conjuncts(on)
                join.set("on", None)
            join.set("kind", None)
            if not sens.of(select):
                join.meta.pop("sql_semantics_join", None)
        if moved:
            where = select.args.get("where")
            select.set("where", exp.Where(this=_and_all((_conjuncts(where.this) if where else []) + moved)))


# ---- 和来源无关的改写

def _items_of(select: Any) -> list[Any]:
    first = select.args.get("from_")
    return ([first.this] if first is not None else []) + [j.this for j in select.args.get("joins") or []]


def _and_all(items: list[Any]) -> Any:
    from sqlglot import exp

    if not items:
        return None
    return exp.and_(*items, copy=False) if len(items) > 1 else items[0]


def _plain_join(join: Any) -> bool:
    return not (join.args.get("using") or join.args.get("method") or join.args.get("natural"))


def _comma(join: Any) -> bool:
    """开头那组内连接里的一项：写成逗号连接、条件都在 WHERE 里（见 _flatten_prefix）。"""
    return _plain_join(join) and not join.side and not join.args.get("kind") and join.args.get("on") is None


def _inner(join: Any) -> bool:
    return _plain_join(join) and not join.side and str(join.kind or "").upper() in ("", "INNER", "CROSS")


def _constant(node: Any) -> bool:
    from sqlglot import exp

    return isinstance(node, (exp.Literal, exp.Null, exp.Boolean)) or (
        isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal))


def _strip_asc(tree: Any) -> None:
    from sqlglot import exp

    for o in tree.find_all(exp.Ordered):
        if o.args.get("desc") is False:
            o.set("desc", None)


def _normalize_negation(tree: Any) -> None:
    """x IS NOT NULL、x NOT LIKE y 这类带 negate 的写法统一写成 NOT (x IS NULL)、NOT (x LIKE y)：各方言解析出来的形状
    不一样（PostgreSQL 的 IS NOT NULL 是 Is(negate=True)，SQLite 的是 Not(Is)），意思一样。"""
    from sqlglot import exp

    for node in list(tree.find_all(exp.Predicate)):
        if node.args.get("negate") is True:
            node.set("negate", None)
            copy = node.copy()
            node.replace(exp.Not(this=exp.Paren(this=copy)))


def _case_forms(tree: Any, read: str | None) -> None:
    """CASE … ELSE NULL END 的 ELSE NULL 去掉（不写 ELSE 就是空值）；SQLite 的 IIF(c, a, b)、MySQL 的 IF(c, a, b) 写成
    CASE WHEN c THEN a ELSE b END（SQLite 文档：两者生成同样的字节码；MySQL 的 IF 在 c 为真时取 a，否则取 b）。"""
    from sqlglot import exp

    if read in ("sqlite", "mysql"):
        for node in list(tree.find_all(exp.If)):
            if isinstance(node.parent, exp.Case) or node.args.get("true") is None:
                continue
            case = exp.Case(ifs=[exp.If(this=node.this.copy(), true=node.args["true"].copy())])
            if node.args.get("false") is not None:
                case.set("default", node.args["false"].copy())
            node.replace(case)
    for case in tree.find_all(exp.Case):
        if isinstance(case.args.get("default"), exp.Null):
            case.set("default", None)


def _drop_true_conjuncts(tree: Any) -> None:
    """WHERE、HAVING、ON 里恒为真的项（TRUE、1 = 1 这种同一个常量和自己相等；分组之后 HAVING COUNT(*) >= 1 这种对
    每一组都成立的）去掉：x AND TRUE 就是 x。全都去掉了的 HAVING 只在已经是聚合查询时去掉（见 _grouped_select）。"""
    from sqlglot import exp

    def true(node: Any) -> bool:
        node = _unparen(node)
        if isinstance(node, exp.Boolean):
            return bool(node.this)
        if isinstance(node, (exp.EQ, exp.GTE, exp.LTE)):
            left, right = _unparen(node.this), _unparen(node.expression)
            return isinstance(left, exp.Literal) and isinstance(right, exp.Literal) and \
                _fingerprint(left) == _fingerprint(right)
        return False

    def always(node: Any, holder: Any) -> bool:
        # 分组之后每组至少一行：HAVING COUNT(*) >= 1、COUNT(*) > 0 对每一组都成立（不分组时没有行也出一组，COUNT(*)
        # 是 0，不算）。按取值逐段算（见 _Range），COUNT(x) 可能是 0，不算
        if not isinstance(holder, exp.Having) or not isinstance(holder.parent, exp.Select):
            return False
        group = holder.parent.args.get("group")
        if group is None or not group.expressions:
            return False
        found = _Range(None).subject(node)
        subject = _unparen(found[1]) if found is not None else None
        if not (isinstance(subject, exp.Count) and isinstance(subject.this, exp.Star)):
            return False
        return _Range(None).compare([node], [], grouped=True) is True

    holders = [(w, "this") for w in tree.find_all(exp.Where, exp.Having)] + \
        [(j, "on") for j in tree.find_all(exp.Join) if j.args.get("on") is not None]
    for holder, key in holders:
        cond = holder.args.get(key)
        parts = _conjuncts(cond)
        kept = [p for p in parts if not true(p) and not always(p, holder)]
        if len(kept) == len(parts):
            continue
        if kept:
            holder.set(key, _and_all([p.copy() for p in kept]))
        elif isinstance(holder, exp.Join):
            holder.set(key, exp.true())
        elif isinstance(holder, exp.Having) and not _grouped_select(holder.parent):
            # 没有 GROUP BY、别处也没有聚合的 HAVING 让整条语句成了只出一行的聚合（PostgreSQL：SELECT 1 FROM orders
            # HAVING 1 = 1 只出一行），MySQL 里又是按行筛：整个去掉就变了，统一写成 HAVING TRUE
            holder.set(key, exp.true())
        else:
            holder.parent.set(holder.arg_key, None)


def _grouped_select(select: Any) -> bool:
    """这一段不看 HAVING 也已经是聚合查询：有 GROUP BY，或者 SELECT 列表、ORDER BY 里有聚合函数（不算窗口函数）。这时
    HAVING TRUE 去不去掉都是那些组。"""
    from sqlglot import exp

    if not isinstance(select, exp.Select):
        return False
    group = select.args.get("group")
    if group is not None and group.expressions:
        return True
    clauses = [*select.expressions, select.args.get("order")]
    return any(not isinstance(n.parent, exp.Window) and n.find_ancestor(exp.Select) is select
               for c in clauses if c is not None for n in c.find_all(exp.AggFunc))


def _count_star(tree: Any) -> None:
    """COUNT(1)、COUNT('x')：数的是不为空的常量，就是行数。COUNT(DISTINCT 1)（有行就是 1）、COUNT(NULL) 不是。"""
    from sqlglot import exp

    for c in list(tree.find_all(exp.Count)):
        if isinstance(c.this, exp.Literal) and not c.args.get("distinct"):
            c.set("this", exp.Star())


def _minmax_distinct(tree: Any, sens: "_Sensitivity | None" = None) -> None:
    """MIN(DISTINCT x)、MAX(DISTINCT x) 就是 MIN(x)、MAX(x)：去不去重最小最大的都是那个值。结果看行出来先后的一段不改
    （带 DISTINCT 的聚合可能换一种执行方式）。"""
    from sqlglot import exp

    for node in list(tree.find_all(exp.Min, exp.Max)):
        owner = node.find_ancestor(exp.Select)
        if sens is not None and owner is not None and sens.of(owner):
            continue
        arg = node.this
        if not node.expressions and isinstance(arg, exp.Distinct) and len(arg.expressions) == 1 and \
                not arg.args.get("on"):
            node.set("this", arg.expressions[0])


def _simple_value(node: Any) -> bool:
    """只是列、常量、* 和它们的加减乘除（没有函数调用：PostgreSQL 里 SELECT 列表的集合返回函数会改变行数）。"""
    from sqlglot import exp

    return all(isinstance(n, (exp.Column, exp.Literal, exp.Null, exp.Boolean, exp.Star, exp.Identifier, exp.Paren,
                              exp.Neg, exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Alias)) for n in node.walk())


def _union_leaves(query: Any) -> list[Any] | None:
    """一串只用 UNION / UNION ALL 连起来、哪一层都不排序不取行的合并的各支；有 INTERSECT / EXCEPT（看的是取值）、
    ORDER BY / LIMIT / OFFSET（取哪几行看去没去重）、WITH 的返回 None。"""
    from sqlglot import exp

    leaves: list[Any] = []
    stack = [query]
    while stack:
        node = stack.pop()
        if isinstance(node, exp.Subquery):
            if node.alias:
                return None
            stack.append(node.this)
        elif isinstance(node, exp.SetOperation):
            if not isinstance(node, exp.Union) or \
                    any(node.args.get(k) is not None for k in ("order", "limit", "offset", "fetch", "with_")):
                return None
            stack += [node.expression, node.this]
        else:
            leaves.append(node)
    return leaves


def _set_union_all(query: Any) -> None:
    """一串 UNION 都改成 UNION ALL（调用方确认了重复的行不影响结果）。"""
    from sqlglot import exp

    for node in [query, *query.find_all(exp.Union)]:
        if isinstance(node, exp.Union):
            node.set("distinct", False)


def _exists_rewritable(sub: Any) -> bool:
    """EXISTS 里的这一段能不能写成 SELECT 1、去掉去重 / 分组 / 排序 / 取前 n 行（见 _exists_subqueries）。"""
    from sqlglot import exp

    if not isinstance(sub, exp.Select):
        return False
    if any(sub.args.get(k) is not None for k in ("having", "offset", "fetch", "qualify")):
        return False
    if any(isinstance(n, exp.AggFunc) and not isinstance(n.parent, exp.Window) for n in _block_nodes(sub)):
        return False
    distinct = sub.args.get("distinct")
    if distinct is not None and distinct.args.get("on"):
        return False
    group = sub.args.get("group")
    if group is not None and any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets", "totals")):
        return False
    limit = sub.args.get("limit")
    if limit is not None:
        value = limit.expression
        if not (isinstance(value, exp.Literal) and value.is_int and int(value.this) >= 1):
            return False
    if not all(_simple_value(p) for p in sub.expressions):
        return False
    # WHERE 里按名字引用了选出来的列别名（SQLite 认：WHERE … AND a2 > 100 里的 a2 是 r.amount * 2 AS a2）：写成
    # SELECT 1 之后这个名字就指不到原来的列了
    return not (_alias_refs(sub) & {p.alias for p in sub.expressions if isinstance(p, exp.Alias) and p.alias})


def _exists_subqueries(tree: Any) -> None:
    """EXISTS 只看有没有行：选什么、去不去重、怎么分组、怎么排都不影响，统一写成 SELECT 1；只取前 n 行（n ≥ 1、不跳过
    行）也不影响有没有行；分组（没有 HAVING、没有聚合）时有行就至少有一组；合并的各支是 UNION 还是 UNION ALL 也不
    影响（有没有行和去不去重无关），各支都写得成 SELECT 1 时一起写。动之前先确认这一段不是聚合查询（SELECT、HAVING、
    ORDER BY 里有聚合函数时总出一行，ORDER BY COUNT(*) 去掉就成了普通查询）、不跳过行（DISTINCT、GROUP BY 在 OFFSET
    之前算，去掉它们行数就变了）、选的只是列和常量（PostgreSQL 的集合返回函数会改变行数）、WHERE 里没按名字引用选出来
    的列别名。"""
    from sqlglot import exp

    for ex in list(tree.find_all(exp.Exists)):
        body = ex.this.this if isinstance(ex.this, exp.Subquery) else ex.this
        if isinstance(body, exp.SetOperation):
            leaves = _union_leaves(body)
            if leaves is None:
                continue
            _set_union_all(body)
        else:
            leaves = [body]
        if not all(_exists_rewritable(leaf) for leaf in leaves):
            continue
        for sub in leaves:
            for key in ("limit", "distinct", "order", "group"):
                sub.set(key, None)
            sub.set("expressions", [exp.Literal.number(1)])


def _in_lists(tree: Any) -> None:
    """IN 列表是个集合：一模一样的项去掉、排序；只有一个常量的写成 =（NOT IN 跟着成了 <>，空值的结果一样）。只有
    一项、又不是常量的不改：SQLite 里 a IN (b) 是 a = +b，右边那一列不带类型亲和，和 a = b 结果不同。"""
    from sqlglot import exp

    for node in list(tree.find_all(exp.In)):
        if not node.expressions or node.args.get("query") or node.args.get("unnest") or node.args.get("field"):
            continue
        uniq = {_fingerprint(i): i for i in node.expressions}
        ordered = [uniq[k] for k in sorted(uniq)]
        if len(ordered) == 1 and _constant(ordered[0]):
            node.replace(exp.EQ(this=node.this, expression=ordered[0]))
        else:
            node.set("expressions", ordered)


def _expand_between(tree: Any, read: str | None, sens: "_Sensitivity | None" = None) -> None:
    """x BETWEEN a AND b 就是 x >= a AND x <= b（SQLite 文档原话，PostgreSQL、MySQL 也是）。x 里有子查询、不确定的
    函数的不改（BETWEEN 只算一次 x）。"""
    from sqlglot import exp

    for node in list(tree.find_all(exp.Between)):
        x = node.this
        if node.args.get("symmetric") or x is None or x.find(exp.Query, exp.Subquery) is not None or _volatile(x):
            continue
        owner = node.find_ancestor(exp.Select)
        if sens is not None and owner is not None and sens.of(owner):
            # 结果看行出来先后的一段不改：一个 BETWEEN 和两个比较估算出的行数可能不同（MySQL 的 condition filtering），
            # 换一种扫描的先后，GROUP_CONCAT 拼出来的先后就跟着变
            continue
        if read == "mysql":
            # MySQL 的 BETWEEN 按三个参数一起定比较的类型，两个比较各定各的：上下界一个数字一个字符串（name BETWEEN 2
            # AND '3'，name = '10' 时 BETWEEN 是 0、两个比较是 1）、或者上下界不是常量（类型不知道）时不改
            low, high = _literal_value(node.args.get("low")), _literal_value(node.args.get("high"))
            if low is None or high is None or low[0] != high[0]:
                continue
        pair = exp.And(this=exp.GTE(this=x.copy(), expression=node.args["low"].copy()),
                       expression=exp.LTE(this=x.copy(), expression=node.args["high"].copy()))
        node.replace(exp.Paren(this=pair))


def _predicate(node: Any) -> bool:
    """取值只会是真、假、空值的条件（比较、IS、IN、LIKE、EXISTS、BETWEEN，和它们的 AND / OR / NOT）。"""
    from sqlglot import exp

    node = _unparen(node)
    if isinstance(node, (exp.Predicate, exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Is, exp.In,
                         exp.Like, exp.ILike, exp.Exists, exp.Between)):
        return True
    if isinstance(node, (exp.And, exp.Or)):
        return _predicate(node.left) and _predicate(node.right)
    if isinstance(node, exp.Not):
        return _predicate(node.this)
    return False


def _fold_not(tree: Any, sens: "_Sensitivity | None" = None) -> None:
    """NOT 往里推（三值逻辑下都成立，两边有空值时结果一样）：NOT 套比较写成反过来的比较（NOT a = b 就是 a <> b，NOT a > b
    就是 a <= b）；NOT NOT p 写成 p（p 是条件时；NOT NOT 5 是 1，不是 5）；NOT (p AND q) 写成 NOT p OR NOT q、NOT (p OR q)
    写成 NOT p AND NOT q（德摩根律，p、q 是条件时；空值 AND / OR 的真值表左右对称，三值逻辑下照样成立）。ANY / ALL 不动；
    结果看行出来先后的一段只做前两样（换了条件的形状可能换一种扫描的先后）。"""
    from sqlglot import exp

    def sensitive(node: Any) -> bool:
        owner = node.find_ancestor(exp.Select)
        return sens is not None and owner is not None and sens.of(owner)

    changed = True
    while changed:
        changed = False
        for node in list(tree.find_all(exp.Not)):
            if node.parent is None and node is not tree:
                continue
            inner = _unparen(node.this)
            name = type(inner).__name__
            if name in _NEGATE and not _quantified(inner.this) and not _quantified(inner.expression):
                node.replace(getattr(exp, _NEGATE[name])(this=inner.this, expression=inner.expression))
                changed = True
            elif isinstance(inner, exp.Not) and _predicate(inner.this):
                node.replace(inner.this)
                changed = True
            elif isinstance(inner, (exp.And, exp.Or)) and _predicate(inner.left) and _predicate(inner.right) and \
                    not sensitive(node):
                left = exp.Not(this=exp.Paren(this=inner.left.copy()))
                right = exp.Not(this=exp.Paren(this=inner.right.copy()))
                flipped = exp.Or(this=left, expression=right) if isinstance(inner, exp.And) else \
                    exp.And(this=left, expression=right)
                node.replace(exp.Paren(this=flipped))
                changed = True
            if changed:
                break


#: 括号里是这些（运算符的一部分）时，外面是比较运算的话括号可以去掉：算术比比较结合得紧
_ARITHMETIC = ("Add", "Sub", "Mul", "Div", "Mod")
_COMPARISON = ("EQ", "NEQ", "GT", "GTE", "LT", "LTE")


def _strip_parens(tree: Any) -> None:
    """不影响结合的括号去掉（比较时本来就不看括号，见 _fingerprint；这里只为生成的写法好读）：括号外面不是运算符
    （函数参数、别名、SELECT 列表、ORDER BY、GROUP BY、WHERE 整个条件、IN 列表的一项……）、括号里是一列一个常量一次
    函数调用，或者括号里是算术、外面是比较。"""
    from sqlglot import exp

    for paren in list(tree.find_all(exp.Paren)):
        inner, parent = paren.this, paren.parent
        if inner is None or parent is None or isinstance(inner, (exp.Query, exp.Subquery, exp.Tuple)):
            continue
        atomic = isinstance(inner, (exp.Column, exp.Literal, exp.Null, exp.Boolean, exp.Paren)) or (
            isinstance(inner, exp.Func) and not isinstance(inner, (exp.Binary, exp.Unary, exp.Predicate)))
        outside = isinstance(parent, (exp.Alias, exp.Distinct, exp.Select, exp.Ordered, exp.Where, exp.Having,
                                      exp.Group, exp.Window)) or (
            isinstance(parent, exp.Func) and not isinstance(parent, (exp.Binary, exp.Unary, exp.Predicate)))
        loose = type(inner).__name__ in _ARITHMETIC and type(parent).__name__ in _COMPARISON
        if isinstance(parent, exp.In) and paren.arg_key == "expressions":
            outside = True
        if atomic or outside or loose:
            paren.replace(inner)


def _zero_offset(tree: Any) -> None:
    from sqlglot import exp

    for offset in list(tree.find_all(exp.Offset)):
        value = offset.expression
        if isinstance(value, exp.Literal) and value.is_int and int(value.this) == 0:
            offset.pop()


def _order_by_aliases(tree: Any) -> None:
    """最外层 ORDER BY 引用 SELECT 的别名的，换成那个表达式：别名接下来要去掉。不换的：别名指的是数字常量（ORDER BY 1
    是按第一列排）；别名指的是一个不带表名、又和某个别名同名的列（ORDER BY 里单独一个名字先认别名：SELECT user_id AS
    amount, amount AS user_id … ORDER BY amount 换成 ORDER BY user_id 就成了按第二列排）。"""
    from sqlglot import exp

    if not isinstance(tree, exp.Select) or tree.args.get("order") is None:
        return
    aliases = {p.alias: p for p in tree.expressions if p.alias}
    names = [p.alias_or_name for p in tree.expressions]
    for o in tree.args["order"].expressions:
        key = o.this
        if not (isinstance(key, exp.Column) and not key.table and key.name in aliases) or names.count(key.name) > 1:
            continue
        target = aliases[key.name].unalias()
        if target.is_number or (isinstance(target, exp.Column) and not target.table and target.name in aliases):
            continue
        o.set("this", target.copy())


def _drop_aliases(query: Any) -> None:
    from sqlglot import exp

    if not isinstance(query, exp.Select):
        return
    used = _alias_refs(query)
    names = [p.alias_or_name for p in query.expressions]
    query.set("expressions", [p.unalias() if isinstance(p, exp.Alias) and p.alias not in used and
                              names.count(p.alias) == 1 else p for p in query.expressions])


def _drop_output_aliases(tree: Any) -> None:
    """最外层（UNION 不按名字排序时各支都是）和条件里的子查询选出来的列名不影响任何取值，去掉；本段里还按名字引用
    的留着。"""
    from sqlglot import exp

    if isinstance(tree, exp.SetOperation):
        if tree.args.get("order") is None:
            for leaf in _leaves(tree):
                _drop_aliases(leaf)
    else:
        _drop_aliases(tree)
    for node in tree.find_all(exp.Exists, exp.In):
        query = node.this if isinstance(node, exp.Exists) else node.args.get("query")
        _drop_aliases(query.this if isinstance(query, exp.Subquery) else query)
    for sub in tree.find_all(exp.Subquery):
        if not isinstance(sub.parent, (exp.From, exp.Join, exp.Exists, exp.In, exp.CTE)) and not sub.alias:
            _drop_aliases(sub.this)


def _leaves(query: Any) -> list[Any]:
    """一棵 UNION / INTERSECT / EXCEPT 树的各支（套着括号的也拆开）。"""
    from sqlglot import exp

    if isinstance(query, exp.Subquery):
        return _leaves(query.this)
    if isinstance(query, exp.SetOperation):
        return _leaves(query.left) + _leaves(query.right)
    return [query]


def _block_nodes(select: Any) -> Iterator[Any]:
    """这一段自己的节点：不进套着的查询；FROM / JOIN 的来源不算（ON 条件算）。"""
    from sqlglot import exp

    stack = [v for k, value in select.args.items() if k not in ("with_", "from_")
             for v in (value if isinstance(value, list) else [value]) if isinstance(v, exp.Expr)]
    while stack:
        node = stack.pop()
        if isinstance(node, (exp.Query, exp.Subquery)):
            continue
        if isinstance(node, exp.Join):
            if node.args.get("on") is not None:
                stack.append(node.args["on"])
            continue
        yield node
        stack += [v for value in node.args.values() for v in (value if isinstance(value, list) else [value])
                  if isinstance(v, exp.Expr)]


def _alias_refs(select: Any) -> set[str]:
    """这一段里可能按名字引用本段列别名的不带表名的名字：WHERE、ON、GROUP BY、HAVING、ORDER BY、窗口里的，以及套在
    本段里的子查询里的全部（SQLite 的 WHERE、ON、相关子查询都认外层的列别名：EXISTS (… WHERE r.amount = b) 里的 b
    是外层 SELECT amount AS b 的 b，在真实的 SQLite 上试过）。SELECT 列表自己的表达式不算（库里不认同一个列表里前面
    的别名），FROM 里的来源不算。改名、去掉列别名之前（列名表写进定义、EXISTS 写成 SELECT 1、去掉没用的列名、CTE 的
    列改叫 c1…）都先看这里，有同名的就不动：不然这些名字就指到别处去了。"""
    from sqlglot import exp

    out: set[str] = set()
    stack: list[tuple[Any, bool]] = []
    for key, value in select.args.items():
        if key in ("with_", "from_"):
            continue
        for v in (value if isinstance(value, list) else [value]):
            if isinstance(v, exp.Expr):
                stack.append((v, key == "expressions"))
    while stack:
        node, projection = stack.pop()
        if isinstance(node, exp.Join):
            if node.args.get("on") is not None:
                stack.append((node.args["on"], False))
            continue
        if isinstance(node, (exp.Query, exp.Subquery)):
            out |= {c.name for c in node.find_all(exp.Column) if not c.table}
            continue
        if isinstance(node, exp.Window):
            out |= {c.name for c in node.find_all(exp.Column) if not c.table}
            continue
        if isinstance(node, exp.Column) and not node.table and not projection:
            out.add(node.name)
        stack += [(child, projection) for child in node.iter_expressions()]
    return out


def _nested_queries(select: Any) -> list[Any]:
    """这一段自己的表达式里直接套着的查询（条件、SELECT 列表、ON 里的子查询），不含 FROM 里的子查询。"""
    from sqlglot import exp

    out: list[Any] = []
    stack = [v for k, value in select.args.items() if k not in ("with_", "from_")
             for v in (value if isinstance(value, list) else [value]) if isinstance(v, exp.Expr)]
    while stack:
        node = stack.pop()
        if isinstance(node, exp.Subquery):
            out.append(node.this)
        elif isinstance(node, exp.Query):
            out.append(node)
        elif isinstance(node, exp.Join):
            if node.args.get("on") is not None:
                stack.append(node.args["on"])
        else:
            stack += [v for value in node.args.values() for v in (value if isinstance(value, list) else [value])
                      if isinstance(v, exp.Expr)]
    return out


def _starry(select: Any) -> bool:
    from sqlglot import exp

    return any(isinstance(p, exp.Star) or (isinstance(p, exp.Column) and p.is_star) for p in select.expressions)


def _alias_or_column(tree: Any, read: str | None, schema: dict[str, dict[str, str]] | None) -> bool:
    """有不带表名、又和这一段的列别名同名的名字，qualify 会把它当成别名展开，库里认的却可能是表的列，这一遍不认列：

    - WHERE、GROUP BY、HAVING 里：库里认的是列还是别名各方言不一样（SQLite 的 GROUP BY、HAVING 都先认列：HAVING
      amount > 10 里的 amount 是组里任意一行的 amount，不是 SUM(amount) AS amount），qualify 却一律当成别名（GROUP BY
      created_at 成了按 DATE(created_at) 分组）。表结构里有这一段的每张表、这个名字又不是其中哪张表的列时，它只能是
      别名，照常认。
    - SELECT 列表里引用前面列的别名：SQLite、PostgreSQL、MySQL 都不认，名字指的是表的列（SELECT user_id AS amount,
      amount * 2 里的 amount 是表的 amount 列），不是表的列就报错；qualify 没有表结构时却当成别名展开成 user_id * 2，
      有表结构时不是表的列的也展开。表结构里有这一段的每张表、这个名字又是其中的列时，qualify 先认列，照常认。"""
    from sqlglot import exp

    columns = {name.lower(): {c.lower() for c in cols} for name, cols in (schema or {}).items()}
    for select in tree.find_all(exp.Select):
        aliases = {p.alias.lower() for p in select.expressions if isinstance(p, exp.Alias) and p.alias}
        if not aliases:
            continue
        items = _items_of(select)
        known = all(isinstance(i, exp.Table) and i.name.lower() in columns for i in items)
        real = set().union(*(columns[i.name.lower()] for i in items)) if known and items else set()
        for key in ("where", "group", "having"):
            node = select.args.get(key)
            if node is None:
                continue
            names = {c.name.lower() for c in node.find_all(exp.Column) if not c.table and not _nested(c, node)}
            if (names & aliases) - (set() if not known else (aliases - real)):
                return True
        earlier: set[str] = set()
        for projection in select.expressions:
            names = {c.name.lower() for c in projection.find_all(exp.Column)
                     if not c.table and (c is projection or not _nested(c, projection))}
            if (names & earlier) - real:
                return True
            if isinstance(projection, exp.Alias) and projection.alias:
                earlier.add(projection.alias.lower())
        # 套在这一段里的子查询用了和这一段列别名同名、不带表名的名字：SQLite 的相关子查询认外层的列别名（EXISTS (…
        # WHERE r.amount = b) 里的 b 是外层 SELECT id AS b 的 id），qualify 却把它归给子查询里的表（r.b），两条把 b 指向
        # 不同列的语句会认成一样
        nested = {c.name.lower() for sub in _nested_queries(select) for c in sub.find_all(exp.Column) if not c.table}
        if nested & aliases:
            return True
    return False


# ---- 起规范的名字

@dataclass
class _Hint:
    """按另一边起名（差异说明用）：定义一样的 CTE 叫另一边的名字；names 是这一边原来的 CTE 名 → 另一边的名字；taken
    是另一边用过的名字，配不上的另起名字时避开。"""

    bodies: dict[str, str] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    taken: set[str] = field(default_factory=set)


class _Namer:
    """给一条语句起规范的名字（见 _finish 第 3 步）。

    先按原来的别名认清每一列属于哪个来源（见 _Resolver）；之后改来源的别名时把这些列一起改掉，不再按名字找，换名不会
    把列认错。别名带层数：内外层的别名不会同名，相关子查询里引用外层的列不会被里层遮住；同一层的兄弟子查询各自看不见
    对方，可以重名。"""

    def __init__(self, tree: Any, read: str | None, qualified: bool, sens: _Sensitivity,
                 hint: _Hint | None = None) -> None:
        from sqlglot import exp

        self.tree = tree
        self.read = read
        self.qualified = qualified
        self.sens = sens
        self.hint = hint
        resolver = _Resolver(tree)
        self.refs = resolver.refs
        self.cte_of = resolver.cte_of
        self.base_names = {t.name.lower() for t in tree.find_all(exp.Table) if t.name}
        self.budget = _ORDERING_BUDGET
        self.starred = self._starred_sources(tree)

    def _starred_sources(self, tree: Any) -> set[int]:
        """被 SELECT * 读着的来源（* 没展开）：它选出来的列名、先后会原样传到外面，不能改名、重排。"""
        from sqlglot import exp

        out: set[int] = set()
        for select in tree.find_all(exp.Select):
            for p in select.expressions:
                if isinstance(p, exp.Star):
                    out |= {id(i) for i in _items_of(select)}
                elif isinstance(p, exp.Column) and p.is_star:
                    out |= {id(i) for i in _items_of(select) if i.alias_or_name == p.table}
        return out

    def _text(self, node: Any) -> tuple[str, str]:
        return _key(node, self.read)

    def _alias(self, base: str, depth: int, k: int) -> str:
        name = base if depth == 0 else f"{base}@{depth}"
        if k > 1:
            name = f"{name}#{k}"
        while (depth or k > 1) and name.lower() in self.base_names:
            name += "#"
        return name

    def canon(self, query: Any, depth: int, cte_names: dict[int, str]) -> tuple[str, str]:
        from sqlglot import exp

        if isinstance(query, exp.Subquery):
            return self.canon(query.this, depth, cte_names)
        with_ = query.args.get("with_")
        if with_ is not None:
            cte_names = dict(cte_names)
            self._name_ctes(with_, depth, cte_names)
        if isinstance(query, exp.SetOperation):
            self._canon_setop(query, depth, cte_names)
            return self._text(query)
        return self._canon_select(query, depth, cte_names)

    def _name_ctes(self, with_: Any, depth: int, cte_names: dict[int, str]) -> None:
        """CTE 按依赖分层，同一层按规范化后的定义排序起名；定义一模一样的合成一个（引用它们的都改叫同一个名字）。"""
        from sqlglot import exp

        hint = self.hint if depth == 0 else None
        pending = list(with_.expressions)
        kept: list[Any] = []
        by_text: dict[tuple[str, str], str] = {}
        prefix = "cte" if depth == 0 else f"cte@{depth}_"
        n = 0
        while pending:
            names = {c.alias_or_name for c in pending}
            ready = [c for c in pending if not any(t.name in names and t.name != c.alias_or_name and not t.args.get("db")
                                                   for t in c.this.find_all(exp.Table))]
            if not ready:
                raise _Unsupported("CTE 互相引用")
            texts = []
            for cte in ready:
                original = cte.alias_or_name
                body = self.canon(cte.this, depth + 1 if depth else 0, cte_names)
                refs = [t for t in self.tree.find_all(exp.Table) if self.cte_of.get(id(t)) is cte]
                if self._rename_outputs(cte.this, refs):
                    body = self._text(cte.this)
                texts.append((body, original, cte))
            texts.sort(key=lambda x: x[0])
            for body, original, cte in texts:
                pending.remove(cte)
                if body in by_text:
                    cte_names[id(cte)] = by_text[body]
                    continue
                name = (hint.names.get(original) or hint.bodies.get(body[0])) if hint else None
                while name is None or name.lower() in self.base_names or name in by_text.values():
                    n += 1
                    name = f"{prefix}{n}"
                    if hint and name in hint.taken:
                        name = None
                cte_names[id(cte)] = name
                by_text[body] = name
                cte.set("alias", exp.TableAlias(this=exp.to_identifier(name)))
                kept.append(cte)
        with_.set("expressions", kept)

    def _rename_outputs(self, body: Any, holders: list[Any]) -> bool:
        """（认列之后）CTE、FROM 里的子查询选出来的列改叫 c1、c2…，引用它们的列跟着改：外面按名字引用，名字是各自起的
        （total 还是 sum_amt）。没有被 SELECT * 原样读着（* 没展开时列名、先后会传到外面）的，先按定义排序：外面按名字
        引用，先后不影响。UNION、选了 *、列名重复、本段里还按名字引用自己的列、GROUP BY / ORDER BY 按位置引用的不改。"""
        from sqlglot import exp

        if not self.qualified or not isinstance(body, exp.Select) or any(id(h) in self.starred for h in holders):
            return False
        projections = body.expressions
        names = [p.alias_or_name for p in projections]
        if not projections or any(not n or _is_star(p) for n, p in zip(names, projections)) or \
                len(set(names)) != len(names):
            return False
        # 本段（含相关子查询）按名字引用了自己的列别名、或者正好用了要起的名字 c1…：改名之后就指到别处了。没写别名
        # 的光一列（SELECT user_id … WHERE user_id > 1）改不改名，WHERE 里的 user_id 都还是那一列
        aliases = {p.alias for p in projections if isinstance(p, exp.Alias) and p.alias}
        if _alias_refs(body) & (aliases | {f"c{i}" for i in range(1, len(projections) + 1)}):
            return False
        for clause in ("group", "order"):
            node = body.args.get(clause)
            keys = [k.this if isinstance(k, exp.Ordered) else k for k in (node.expressions if node is not None else [])]
            if any(k.is_number for k in keys):
                return False
        ordered = sorted(projections, key=lambda p: self._text(p.unalias()))
        mapping = {p.alias_or_name: f"c{i}" for i, p in enumerate(ordered, 1)}
        body.set("expressions", [exp.alias_(p.unalias().copy(), mapping[p.alias_or_name], copy=False)
                                 for p in ordered])
        for holder in holders:
            for col in self.refs.get(id(holder), []):
                if col.name in mapping:
                    col.set("this", exp.to_identifier(mapping[col.name]))
        return True

    def _canon_setop(self, query: Any, depth: int, cte_names: dict[int, str]) -> None:
        """连着同一种合并写的几支各自规范化；调换先后不改变结果的（见 _reorderable）再按规范化后的写法排序。"""
        kind = (type(query), bool(query.args.get("distinct")))
        branches: list[Any] = []

        def collect(node: Any) -> None:
            from sqlglot import exp

            if isinstance(node, exp.SetOperation) and (type(node), bool(node.args.get("distinct"))) == kind and \
                    not any(node.args.get(k) for k in ("order", "limit", "offset", "with_")):
                collect(node.left)
                collect(node.right)
            else:
                branches.append(node)

        collect(query.left)
        collect(query.right)
        texts = [(self.canon(b, depth, cte_names), i, b) for i, b in enumerate(branches)]
        if len(branches) < 2 or not self._reorderable(query):
            return
        ordered = [b for _, _, b in sorted(texts, key=lambda x: (x[0], x[1]))]
        node = ordered[0]
        for b in ordered[1:-1]:
            node = type(query)(this=node, expression=b, distinct=query.args.get("distinct"))
        query.set("this", node)
        query.set("expression", ordered[-1])

    def _reorderable(self, query: Any) -> bool:
        """这一处合并的各支调换先后，结果（行、值）一样不一样。只认整条语句最外层、不排序不取行的 UNION ALL：

        - UNION（去重）、INTERSECT 留先出现的那个值，相等不等于一样（SQLite 里 REAL 列的 2.0 和 INTEGER 列的 2、MySQL
          默认不分大小写的 'SH' 和 'sh'）；EXCEPT 本来就分左右。
        - 带 LIMIT / OFFSET 的取的是先出来的几行；ORDER BY 按名字排的看第一支的列名；SQLite 给合并的结果排序时用最左
          一支那列声明的排序规则。PostgreSQL、MySQL 按位置排序（ORDER BY 1）的可以排。
        - 套在 CTE、子查询里的：SQLite 从子查询读合并的结果时按第一支的类型亲和、排序规则转换别的支，不排；别的方言
          只在各支选出来的列名、先后都一样（外面按列名引用，列名取第一支的）、外面不看行出来的先后（不去重、不取行，
          见 _Sensitivity）时排。"""
        from sqlglot import exp

        if not isinstance(query, exp.Union) or query.args.get("distinct"):
            return False
        if any(query.args.get(k) is not None for k in ("limit", "offset", "fetch")):
            return False
        sqlite = self.read in (None, "sqlite")
        order = query.args.get("order")
        if order is not None and (sqlite or any(not (isinstance(o.this, exp.Literal) and o.this.is_int)
                                                for o in order.expressions)):
            return False
        top = query
        while isinstance(top.parent, exp.Subquery):
            top = top.parent
        if top is self.tree:
            return True
        if sqlite or order is not None:
            return False
        leaves = _leaves(query)
        if any(not isinstance(leaf, exp.Select) or self.sens.of(leaf) or _starry(leaf) for leaf in leaves):
            return False
        names = [[p.alias_or_name for p in leaf.expressions] for leaf in leaves]
        return all(n == names[0] for n in names) and all(names[0])

    def _signature(self, item: Any, cte_names: dict[int, str], subs: dict[int, tuple[str, str]]) -> tuple[str, str]:
        """来源的 (排序用的标识, 起别名用的名字)：实表是表名，CTE 是它的规范名，FROM 里的子查询是规范化后的写法。"""
        from sqlglot import exp

        if isinstance(item, exp.Table):
            cte = self.cte_of.get(id(item))
            if cte is not None:
                name = cte_names.get(id(cte), cte.alias_or_name)
                return f"cte:{name}", name
            return "table:" + ".".join(p for p in (item.catalog, item.db, item.name) if p), item.name
        return f"sub:{subs[id(item)][0]}", "sub"

    def _canon_select(self, select: Any, depth: int, cte_names: dict[int, str]) -> tuple[str, str]:
        from sqlglot import exp

        items = _items_of(select)
        subs: dict[int, tuple[str, str]] = {}
        for item in items:
            if isinstance(item, exp.Subquery):
                subs[id(item)] = self.canon(item.this, depth + 1, cte_names)
                if self._rename_outputs(item.this, [item]):
                    subs[id(item)] = self._text(item.this)
            elif id(item) in self.cte_of:
                cte = self.cte_of[id(item)]
                item.set("this", exp.to_identifier(cte_names.get(id(cte), cte.alias_or_name)))
        keys = {id(i): self._signature(i, cte_names, subs) for i in items}
        joins = select.args.get("joins") or []
        n_prefix = 0
        if items:
            n_prefix = 1
            for join in joins:
                if _comma(join):
                    n_prefix += 1
                else:
                    break
        prefix, rest = items[:n_prefix], joins[max(n_prefix - 1, 0):] if items else joins
        # 后面有 RIGHT / FULL JOIN 时开头那组来源不调换：PostgreSQL、MySQL 里 a, b RIGHT JOIN c 是 a 乘上 (b RIGHT JOIN c)，
        # 换成 b, a 就成了 b 乘上 (a RIGHT JOIN c)
        movable = not _bare_star(select) and not self.sens.of(select) and \
            not any(str(j.side or "").upper() in ("RIGHT", "FULL") for j in joins)
        free_lefts = movable and bool(rest) and self._independent_lefts(prefix, rest)

        def groups(xs: list[Any], key: Any) -> list[list[Any]]:
            xs = sorted(xs, key=key)
            return [list(g) for _, g in itertools.groupby(xs, key=key)]

        prefix_groups = groups(prefix, lambda s: keys[id(s)][0]) if movable else [[s] for s in prefix]
        rest_groups = groups(rest, lambda j: keys[id(j.this)][0]) if free_lefts else [[j] for j in rest]
        total = 1
        for g in prefix_groups + rest_groups:
            total *= math.factorial(len(g))
            if total > _MAX_ORDERINGS:
                break
        if total > _MAX_ORDERINGS or total > self.budget:
            # 排列太多（十几个同名来源）：先算个数再展开，超了就只按排好的先后
            options = [[g] for g in prefix_groups + rest_groups]
            total = 1
        else:
            options = [[list(p) for p in itertools.permutations(g)] if len(g) > 1 else [g]
                       for g in prefix_groups + rest_groups]
        self.budget -= total
        combos = list(itertools.product(*options))
        best: tuple[tuple[str, str], int] | None = None
        for index, combo in enumerate(combos):
            text = self._apply(select, depth, cte_names, keys, combo, len(prefix_groups))
            if best is None or text < best[0]:
                best = (text, index)
        if best is None:
            return self._text(select)
        if len(combos) > 1:
            self._apply(select, depth, cte_names, keys, combos[best[1]], len(prefix_groups))
        return best[0]

    @staticmethod
    def _independent_lefts(prefix: list[Any], rest: list[Any]) -> bool:
        """跟在一组内连接后面的几个 LEFT JOIN，各自的 ON 只引用这组来源和自己（不引用别的 LEFT JOIN 进来的表、没有
        子查询）：它们之间谁先谁后结果一样。"""
        from sqlglot import exp

        names = {s.alias_or_name for s in prefix}
        for join in rest:
            if str(join.side or "").upper() != "LEFT" or not _plain_join(join):
                return False
            on = join.args.get("on")
            if on is None or on.find(exp.Select, exp.Subquery) is not None:
                return False
            if not {c.table for c in on.find_all(exp.Column)} <= names | {join.this.alias_or_name}:
                return False
        return True

    def _apply(self, select: Any, depth: int, cte_names: dict[int, str], keys: dict[int, tuple[str, str]],
               combo: tuple[list[Any], ...], n_prefix_groups: int) -> tuple[str, str]:
        from sqlglot import exp

        prefix = [s for g in combo[:n_prefix_groups] for s in g]
        rest = [j for g in combo[n_prefix_groups:] for j in g]
        if prefix:
            select.args["from_"].set("this", prefix[0])
            select.set("joins", [exp.Join(this=s) for s in prefix[1:]] + rest or None)
        counts: dict[str, int] = {}
        for item in _items_of(select):
            _, base = keys[id(item)]
            counts[base] = counts.get(base, 0) + 1
            name = self._alias(base, depth, counts[base])
            item.set("alias", exp.TableAlias(this=exp.to_identifier(name)))
            for col in self.refs.get(id(item), []):
                col.set("table", exp.to_identifier(name))
        for sub in _nested_queries(select):
            self.canon(sub, depth + 1, cte_names)
        _sort_own(select, self.read, self.sens.of(select))
        return self._text(select)


def _fixed(node: Any) -> bool:
    """不涉及任何列、子查询的表达式（常量、常量算出来的日期）：排比较、加乘的两边时放在右边，好读。"""
    from sqlglot import exp

    return node.find(exp.Column, exp.Select, exp.Subquery) is None


def _sort_own(select: Any, read: str | None, sensitive: bool = False) -> None:
    """这一段自己的表达式里交换律成立的地方排好（里层先排）：AND、OR（各项都是条件时去掉一模一样的项：x AND x 在
    SELECT 列表里不是 x）、比较（不涉及列的一边在右，两边都涉及列时排好、大于小于跟着换方向；两边都写了 COLLATE 的
    不换，SQLite 用左边那个）、两项的加和乘（只换这一个节点的两边，不跨项挪动：浮点数加法不满足结合律）、IN 列表、
    GROUP BY（结果看行出来的先后时不排：SQLite 按分组键排好了出行）。"""
    from sqlglot import exp

    def key(node: Any) -> tuple[str, str]:
        return _key(node, read)

    def depth(node: Any) -> int:
        d = 0
        while node is not select and node.parent is not None:
            node = node.parent
            d += 1
        return d

    for node in sorted(_block_nodes(select), key=depth, reverse=True):
        if node is not select and node.parent is None:
            continue
        if isinstance(node, (exp.And, exp.Or)):
            if isinstance(_unparen_parent(node), type(node)):
                continue
            parts = _flat(node, type(node))
            if all(_predicate(p) for p in parts) and not sensitive:
                # 一模一样的条件去掉一个。结果看行出来先后的一段不去：MySQL 按条件估算行数，同一个条件写两遍就换了一种
                # 关联的先后，GROUP_CONCAT 拼出来的先后跟着变（第 3 轮在真实的 MySQL 8.0 上压测时看到的）
                uniq: dict[tuple[str, str], Any] = {}
                for part in parts:
                    uniq.setdefault(key(part), part)
                ordered = [uniq[k] for k in sorted(uniq)]
            else:
                ordered = sorted(parts, key=key)
            new = ordered[0] if len(ordered) == 1 else (
                exp.and_(*ordered, copy=False) if isinstance(node, exp.And) else exp.or_(*ordered, copy=False))
            node.replace(new)
        elif type(node).__name__ in _FLIP:
            left, right = node.this, node.expression
            if _quantified(left) or _quantified(right):
                continue
            if left.find(exp.Collate) is not None and right.find(exp.Collate) is not None:
                continue
            if (_fixed(left) and not _fixed(right)) or (_fixed(left) == _fixed(right) and key(left) > key(right)):
                node.replace(getattr(exp, _FLIP[type(node).__name__])(this=right, expression=left))
        elif isinstance(node, (exp.Add, exp.Mul)):
            left, right = node.this, node.expression
            if (_fixed(left) and not _fixed(right)) or (_fixed(left) == _fixed(right) and key(left) > key(right)):
                node.set("this", right)
                node.set("expression", left)
        elif isinstance(node, exp.Group):
            # 一模一样的分组键写两遍和写一遍分出的组一样；结果看行出来先后的一段不动（分组键变了可能换一种扫描的先后）
            if node.expressions and not sensitive:
                uniq_keys: dict[tuple[str, str], Any] = {}
                for k in node.expressions:
                    uniq_keys.setdefault(key(k), k)
                node.set("expressions", sorted(uniq_keys.values(), key=key))
        elif isinstance(node, exp.In) and node.expressions:
            uniq = {key(i): i for i in node.expressions}
            node.set("expressions", [uniq[k] for k in sorted(uniq)])


def _flat(node: Any, cls: type) -> list[Any]:
    """AND / OR 连着的各项（穿过括号：BETWEEN 改写出来的 (x >= a AND x <= b) 也拆开）。"""
    out: list[Any] = []
    stack = [node]
    while stack:
        n = stack.pop()
        inner = _unparen(n)
        if isinstance(inner, cls):
            stack += [inner.expression, inner.this]
        else:
            out.append(n)
    return out


def _unparen_parent(node: Any) -> Any:
    from sqlglot import exp

    parent = node.parent
    while isinstance(parent, exp.Paren):
        parent = parent.parent
    return parent


# ==========================================================================
# 差异说明
# ==========================================================================


@dataclass(frozen=True)
class FacetDiff:
    """一处不同。aspect：time（时间列）/ time_range（同一时间列、范围不同）/ filter（筛选条件）/ aggregate（聚合函数、
    去重计数）/ expression（选出来的列）/ join（关联的表、关联类型、关联条件、外连接的主表）/ tables（查的是哪张表）/
    set_op（几支查询怎么合并）/ group_by（分组键、去重）/ having / limit（取哪几行，含取行前的排序）/ order（只是最终
    结果的排序不同，不影响取到哪些行）/ parse（解析不了）/ other（兜底：未能归纳成具体差异，见 explain_sql_diff）。a、b
    是两边各自的写法（给人看，没有就是 None），text 是一句中文说明。detail 只在 time 上有：两边的时间列
    [{table, column}]（各是各的表），调用方据此对照数据目录（业务日期）；在 filter、time_range 上有 {"columns":
    [{table, column}]}：条件只涉及的实表列，对照有效行过滤用。filter 一边是 None 的是「一边多出来的条件」；两边都有的是
    同一列上的条件两边各写了一套（「A 对 X 的条件是 …，B 是 …」），不是一边多出来的有效行条件。"""

    aspect: str
    a: Any
    b: Any
    text: str
    detail: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        out = {"aspect": self.aspect, "a": self.a, "b": self.b, "text": self.text}
        if self.detail is not None:
            out["detail"] = self.detail
        return out


@dataclass(frozen=True)
class SqlDiff:
    """两条 SQL 差在哪（explain_sql_diff 的结果）。

    equivalent：两边经过只保持语义的变换之后，规范形式一字不差（level 是哪一级的规范形式，见 _canonical_forms）。
    items：具体差异，等价时为空；不等价时一定不为空——归纳不出具体差异时是一条 aspect="other" 的兜底说明。
    fallback：用了兜底说明（一条具体说法都没有，或者具体说法只覆盖了一部分）。
    text_diff：规范化之后两条 SQL 不同的行（difflib 比两边 sqlglot pretty 的输出，WHERE、ON、HAVING 的合取式每个条件
    一行，「- 」开头是 A 的、「+ 」开头是 B 的；不同的行前面带上它的各级上级行，「  」开头、两边相同，只为标出不同的行
    在哪个子句里；最多 40 行，见 _text_diff），给界面高亮；等价时为空。"""

    equivalent: bool
    items: list[FacetDiff] = field(default_factory=list)
    fallback: bool = False
    text_diff: tuple[str, ...] = ()
    level: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"equivalent": self.equivalent, "items": [d.as_dict() for d in self.items], "fallback": self.fallback,
                "text_diff": list(self.text_diff), "level": self.level}


#: 兜底说明
_FALLBACK = "两条 SQL 的写法不同，未能归纳成具体差异，请对照原文"
_FALLBACK_REST = "另有未能归纳的差异，请对照原文"
#: text_diff 最多列几行
_MAX_DIFF_LINES = 40
#: 逐行比较时不看的、条件前面的 AND（见 _text_diff）
_LEADING_AND = re.compile(r"^(\s*)AND ")
#: 各方言「每个条件一行」的生成器类，按 sqlglot 的生成器类缓存（见 _line_generator）
_LINE_GENERATORS: dict[type, type] = {}

_CJK_PUNCT = frozenset("，。；：、！？「」『』（）《》【】…—")
#: 聚合函数的说法
_VERBS = {"sum": "求和", "count": "计数", "avg": "求平均", "min": "取最小值", "max": "取最大值"}
#: 只取日期的字符串（2026-09-01）：PostgreSQL、MySQL 里拿它和时间列比，按字符串比的先后和按日期比的一样
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_COMPARE_OPS = ("EQ", "NEQ", "GT", "GTE", "LT", "LTE")
#: LIKE 的对象的两种取值（见 _Range.always）：匹配上模式的串、匹配不上的串
_LIKE_MATCH, _LIKE_MISS = "\x00like:match", "\x00like:miss"
#: 判断恒为真时最多试多少种取值的组合：再多就当认不出（只会少说）
_MAX_COMBINATIONS = 20000


def _wide(ch: str) -> bool:
    return "⺀" <= ch <= "鿿" or "豈" <= ch <= "﫿" or "＀" <= ch <= "￯"


def _say(*parts: str) -> str:
    """拼一句话：汉字和英文、数字、SQL 之间加一个空格，挨着全角标点的不加，汉字和汉字之间不加。

    两边的叫法由调用方定（A / B，或者「原流程」「盲算」），所以空格只能拼的时候按字判断。
    """
    out = ""
    for part in parts:
        if not part:
            continue
        if out and not out[-1].isspace() and not part[0].isspace():
            a, b = out[-1], part[0]
            if not (a in _CJK_PUNCT or b in _CJK_PUNCT or (_wide(a) and _wide(b))):
                out += " "
        out += part
    return out


def catalog_labels(notes: Mapping[str, Mapping[str, Any]] | None) -> dict[str, str]:
    """数据目录（{表名: notes}）里列的中文名 → diff_facets / explain_sql_diff 的 names：{「表.列」: 中文名}；只在一张
    表里有、或者各表叫法一致的列另记 {列: 中文名}。被驳回的项不算。"""
    out: dict[str, str] = {}
    bare: dict[str, tuple[str, set[str]]] = {}
    for table, raw in (notes or {}).items():
        visible = catalog.visible_notes(raw) if isinstance(raw, Mapping) else {}
        columns = visible.get("columns") if isinstance(visible.get("columns"), Mapping) else {}
        for column, items in columns.items():
            item = items.get("label") if isinstance(items, Mapping) else None
            label = item.get("value") if isinstance(item, Mapping) else None
            if isinstance(label, str) and label.strip():
                out[f"{table}.{column}"] = label.strip()
                bare.setdefault(name_key(str(column)), (str(column), set()))[1].add(label.strip())
    for column, labels in bare.values():
        if len(labels) == 1:
            out.setdefault(column, next(iter(labels)))
    return out


def _labeler(names: Mapping[str, str] | None) -> Any:
    index = {name_key(str(k)): str(v) for k, v in (names or {}).items() if isinstance(v, str) and v.strip()}

    def label(ref: ColumnRef) -> str | None:
        table, column = ref
        return (index.get(name_key(f"{table}.{column}")) if table else None) or index.get(name_key(column))

    return label


# ---- 同一个对象上的条件：留下的取值一样不一样

def _literal_value(node: Any) -> tuple[str, Any] | None:
    """常量的值：("n", Decimal) 或 ("s", 字符串)；不是数字、字符串常量返回 None。"""
    from decimal import Decimal, InvalidOperation

    from sqlglot import exp

    node = _unparen(node)
    if isinstance(node, exp.Neg):
        inner = _literal_value(node.this)
        return ("n", -inner[1]) if inner is not None and inner[0] == "n" else None
    if isinstance(node, exp.Literal):
        if node.is_string:
            return "s", str(node.this)
        try:
            return "n", Decimal(str(node.this))
        except (InvalidOperation, ValueError):
            return None
    return None


class _Range:
    """一个对象（一列、一个聚合、一个函数）上只和常量比的条件（=、<>、<、<=、>、>=、IN 常量列表、IS [NOT] NULL、
    BETWEEN，用 AND / OR / NOT 连起来），在对象的各段取值上成立不成立：用到的常量把取值切成一段段（小于最小的、
    每个常量本身、相邻两个之间、大于最大的），每段里条件的真假都一样，各取一个代表算一遍，再算对象是空值时。两组
    条件在每一段、空值上都同真同不真，留下的行就一样。

    字符串的大小先后：SQLite 按默认的 BINARY 排序规则；PostgreSQL（语言排序规则下 'a' < 'B'）、MySQL（默认不分
    大小写）只认等不等，除非常量都是只取日期的字符串（2026-09-01，按字符串比和按日期比先后一样）。MySQL 的字符串
    等不等不分大小写。COUNT 的取值是整数（分组时至少是 1）。"""

    def __init__(self, read: str | None) -> None:
        self.read = read

    def subject(self, cond: Any) -> tuple[str, Any, list[tuple[str, Any]], bool] | None:
        """cond 只是同一个对象和常量比时，返回 (对象的指纹, 对象, 常量, 有没有比大小)；不然 None。"""
        from sqlglot import exp

        subjects: dict[str, Any] = {}
        literals: list[tuple[str, Any]] = []
        ranged = [False]

        def side(subj: Any, lits: list[Any]) -> bool:
            if subj is None or _literal_value(subj) is not None or isinstance(_unparen(subj), exp.Null) or \
                    subj.find(exp.Query, exp.Subquery) is not None or _volatile(subj):
                return False
            subjects[_fingerprint(subj)] = subj
            literals.extend(lits)
            return True

        def walk(n: Any) -> bool:
            n = _unparen(n)
            if isinstance(n, (exp.And, exp.Or)):
                return walk(n.left) and walk(n.right)
            if isinstance(n, exp.Not):
                return walk(n.this)
            name = type(n).__name__
            if name in _COMPARE_OPS:
                lv, rv = _literal_value(n.expression), _literal_value(n.this)
                if name not in ("EQ", "NEQ"):
                    ranged[0] = True
                if lv is not None and rv is None:
                    return side(n.this, [lv])
                if rv is not None and lv is None:
                    return side(n.expression, [rv])
                return False
            if isinstance(n, exp.In):
                if n.args.get("query") or n.args.get("unnest") or n.args.get("field") or not n.expressions:
                    return False
                lits = []
                for item in n.expressions:
                    if isinstance(_unparen(item), exp.Null):
                        continue
                    value = _literal_value(item)
                    if value is None:
                        return False
                    lits.append(value)
                return side(n.this, lits)
            if isinstance(n, exp.Is):
                return isinstance(_unparen(n.expression), exp.Null) and side(n.this, [])
            if n.args.get("negate"):
                return False
            if isinstance(n, exp.Between):
                low, high = _literal_value(n.args.get("low")), _literal_value(n.args.get("high"))
                ranged[0] = True
                return not n.args.get("symmetric") and low is not None and high is not None and \
                    side(n.this, [low, high])
            return False

        if not walk(cond) or len(subjects) != 1:
            return None
        kinds = {k for k, _ in literals}
        if len(kinds) > 1:
            return None
        if ranged[0] and kinds == {"s"} and self.read != "sqlite" and \
                not all(_DATE_ONLY.match(v) for _, v in literals):
            return None
        fp, subj = next(iter(subjects.items()))
        return fp, subj, literals, ranged[0]

    def _norm(self, value: tuple[str, Any]) -> Any:
        """MySQL 默认的排序规则不分大小写、比较时不看末尾的空格（PAD SPACE：'paid' = 'paid '），按这个规则比的值。"""
        kind, v = value
        if kind == "s" and self.read == "mysql":
            return v.casefold().rstrip(" ")
        return v

    def _pieces(self, subject: Any, literals: list[tuple[str, Any]], ranged: bool, grouped: bool) -> list[Any]:
        """代表各段取值的值（不含空值）。"""
        from decimal import Decimal

        from sqlglot import exp

        values = sorted({self._norm(v) for v in literals})
        kind = literals[0][0] if literals else "n"
        if not values:
            return [Decimal(0)] if kind == "n" else ["x"]
        if not ranged:
            other = values[-1] + 1 if kind == "n" else values[-1] + "\x00~"
            return values + [other]
        out: list[Any] = []
        if kind == "n":
            integral = isinstance(_unparen(subject), exp.Count)
            low = Decimal(1) if integral and grouped and isinstance(_unparen(subject).this, exp.Star) else (
                Decimal(0) if integral else None)
            candidates = [values[0] - 1] + [x for i, v in enumerate(values)
                                             for x in ([v] + ([(v + values[i + 1]) / 2] if i + 1 < len(values) else []))]
            candidates.append(values[-1] + 1)
            if integral:
                ints: set[Decimal] = set()
                for i, v in enumerate(values):
                    ints |= {v.to_integral_value(rounding="ROUND_FLOOR") + d for d in (-1, 0, 1)}
                    ints |= {v.to_integral_value(rounding="ROUND_CEILING") + d for d in (-1, 0, 1)}
                candidates = sorted(ints)
            for c in candidates:
                if low is not None and c < low:
                    continue
                out.append(c)
            return out or [low or Decimal(0)]
        if values[0] != "":
            out.append("")
        for i, v in enumerate(values):
            out.append(v)
            between = v + "\x00"
            if i + 1 >= len(values) or between < values[i + 1]:
                out.append(between)
        return out

    def _truth(self, n: Any, v: Any) -> str:
        """n 在对象取 v（None 是空值）时是真（T）、假（F）还是空值（N）。"""
        from sqlglot import exp

        n = _unparen(n)
        if isinstance(n, exp.And):
            a, b = self._truth(n.left, v), self._truth(n.right, v)
            return "F" if "F" in (a, b) else ("N" if "N" in (a, b) else "T")
        if isinstance(n, exp.Or):
            a, b = self._truth(n.left, v), self._truth(n.right, v)
            return "T" if "T" in (a, b) else ("N" if "N" in (a, b) else "F")
        if isinstance(n, exp.Not):
            t = self._truth(n.this, v)
            return {"T": "F", "F": "T"}.get(t, "N")
        if isinstance(n, exp.Is):
            hit = v is None
            return "T" if hit != bool(n.args.get("negate")) else "F"
        if v is None:
            return "N"
        name = type(n).__name__
        if name in _COMPARE_OPS:
            right = _literal_value(n.expression)
            if right is not None:
                lit, op = self._norm(right), name
            else:
                lit, op = self._norm(_literal_value(n.this)), _FLIP[name]
            if type(v) is not type(lit):
                return "N"
            result = {"EQ": v == lit, "NEQ": v != lit, "GT": v > lit, "GTE": v >= lit, "LT": v < lit,
                      "LTE": v <= lit}[op]
            return "T" if result else "F"
        if isinstance(n, exp.In):
            hit = any(not isinstance(_unparen(i), exp.Null) and self._norm(_literal_value(i)) == v
                      for i in n.expressions)
            if hit:
                return "T"
            return "N" if any(isinstance(_unparen(i), exp.Null) for i in n.expressions) else "F"
        if isinstance(n, exp.Between):
            low, high = self._norm(_literal_value(n.args["low"])), self._norm(_literal_value(n.args["high"]))
            return "T" if low <= v <= high else "F"
        return "N"

    def satisfiable(self, conds: list[Any], grouped: bool = False, never_null: bool = False) -> bool | None:
        """同一个对象只和常量比的几个条件（AND 连）有没有一个取值（含空值，never_null 时不含）让它们都成立；认不出
        返回 None。"""
        from sqlglot import exp

        parsed = [self.subject(c) for c in conds]
        if not parsed or any(p is None for p in parsed) or len({p[0] for p in parsed}) != 1:
            return None
        literals = [lit for p in parsed for lit in p[2]]
        if len({k for k, _ in literals}) > 1:
            return None
        subject = parsed[0][1]
        values: list[Any] = self._pieces(subject, literals, any(p[3] for p in parsed), grouped)
        if not isinstance(_unparen(subject), exp.Count) and not never_null:
            values.append(None)
        return any(all(self._truth(c, v) == "T" for c in conds) for v in values)

    def _atoms(self, cond: Any) -> list[tuple[str, Any, list[tuple[str, Any]], bool, str | None]] | None:
        """条件拆成「一个对象和常量比」的几项（AND / OR / NOT 连起来）：[(对象的指纹, 对象, 常量, 有没有比大小, LIKE 的
        模式)]；有别的写法（两个对象比、子查询、函数套着条件……）返回 None。"""
        from sqlglot import exp

        out: list[tuple[str, Any, list[tuple[str, Any]], bool, str | None]] = []

        def side(subj: Any, lits: list[Any], ranged: bool, like: str | None = None) -> bool:
            if subj is None or _literal_value(subj) is not None or isinstance(_unparen(subj), exp.Null) or \
                    subj.find(exp.Query, exp.Subquery) is not None or _volatile(subj):
                return False
            out.append((_fingerprint(subj), subj, lits, ranged, like))
            return True

        def walk(n: Any) -> bool:
            n = _unparen(n)
            if isinstance(n, (exp.And, exp.Or)):
                return walk(n.left) and walk(n.right)
            if isinstance(n, exp.Not):
                return walk(n.this)
            name = type(n).__name__
            if name in _COMPARE_OPS:
                lv, rv = _literal_value(n.expression), _literal_value(n.this)
                ranged = name not in ("EQ", "NEQ")
                if lv is not None and rv is None:
                    return side(n.this, [lv], ranged)
                if rv is not None and lv is None:
                    return side(n.expression, [rv], ranged)
                return False
            if isinstance(n, exp.In):
                if n.args.get("query") or n.args.get("unnest") or n.args.get("field") or not n.expressions:
                    return False
                lits = []
                for item in n.expressions:
                    if isinstance(_unparen(item), exp.Null):
                        continue
                    value = _literal_value(item)
                    if value is None:
                        return False
                    lits.append(value)
                return side(n.this, lits, False)
            if isinstance(n, exp.Is):
                return isinstance(_unparen(n.expression), exp.Null) and side(n.this, [], False)
            if n.args.get("negate"):
                return False
            if isinstance(n, exp.Between):
                low, high = _literal_value(n.args.get("low")), _literal_value(n.args.get("high"))
                return not n.args.get("symmetric") and low is not None and high is not None and \
                    side(n.this, [low, high], True)
            if isinstance(n, (exp.Like, exp.ILike)):
                pattern = _unparen(n.expression)
                return isinstance(pattern, exp.Literal) and pattern.is_string and \
                    side(n.this, [], False, str(pattern.this))
            return False

        return out if walk(cond) else None

    def _analyze(self, conds: list[Any]) -> dict[str, dict[str, Any]] | None:
        per: dict[str, dict[str, Any]] = {}
        for c in conds:
            atoms = self._atoms(c)
            if atoms is None:
                return None
            for fp, subj, lits, ranged, like in atoms:
                entry = per.setdefault(fp, {"node": subj, "lits": [], "ranged": False, "likes": set(), "plain": 0})
                entry["lits"] += lits
                entry["ranged"] |= ranged
                if like is None:
                    entry["plain"] += 1
                else:
                    entry["likes"].add(like)
        for entry in per.values():
            kinds = {k for k, _ in entry["lits"]}
            # LIKE 只能单独出现在一个对象上（x LIKE 'a%' OR x NOT LIKE 'a%' 这类和别的条件交错的算不清）
            if (entry["likes"] and (entry["plain"] or len(entry["likes"]) > 1)) or len(kinds) > 1:
                return None
            if entry["ranged"] and kinds == {"s"} and self.read != "sqlite" and \
                    not all(_DATE_ONLY.match(v) for _, v in entry["lits"]):
                return None
        return per

    def subjects(self, conds: list[Any]) -> dict[str, Any] | None:
        """几个条件（每个都由「对象和常量比」用 AND / OR / NOT 连起来）里拿来比的对象 {指纹: 对象}；认不出返回 None。"""
        per = self._analyze(conds)
        return None if per is None else {fp: entry["node"] for fp, entry in per.items()}

    def always(self, conds: list[Any], never_null: Iterable[str] = ()) -> bool | None:
        """几个条件（AND 连）是不是恒为真：每个对象按自己用到的常量切成几段取值（见 _pieces，LIKE 的对象分「匹配上」
        「匹配不上」两种，只由 % 组成的模式不为空就一定匹配上），加上空值（never_null 里的对象不取空值），各个对象的
        取值逐个组合算一遍，都是真才是恒为真。组合太多、认不出时返回 None。"""
        import itertools

        from sqlglot import exp

        per = self._analyze(conds)
        if per is None:
            return None
        skip = set(never_null)
        domains: list[list[Any]] = []
        for fp, entry in per.items():
            if entry["likes"]:
                (pattern,) = entry["likes"]
                values: list[Any] = [_LIKE_MATCH] if pattern and set(pattern) <= {"%"} else [_LIKE_MATCH, _LIKE_MISS]
            else:
                values = self._pieces(entry["node"], entry["lits"], entry["ranged"], False)
            if fp not in skip and not isinstance(_unparen(entry["node"]), exp.Count):
                values = values + [None]
            domains.append(values)
        if math.prod(len(d) for d in domains) > _MAX_COMBINATIONS:
            return None
        names = list(per)
        for combo in itertools.product(*domains):
            assign = dict(zip(names, combo))
            if any(self._truth_at(c, assign) != "T" for c in conds):
                return False
        return True

    def _truth_at(self, n: Any, assign: Mapping[str, Any]) -> str:
        """n 在各对象取 assign 里的值时是真（T）、假（F）还是空值（N）。"""
        from sqlglot import exp

        n = _unparen(n)
        if isinstance(n, exp.And):
            a, b = self._truth_at(n.left, assign), self._truth_at(n.right, assign)
            return "F" if "F" in (a, b) else ("N" if "N" in (a, b) else "T")
        if isinstance(n, exp.Or):
            a, b = self._truth_at(n.left, assign), self._truth_at(n.right, assign)
            return "T" if "T" in (a, b) else ("N" if "N" in (a, b) else "F")
        if isinstance(n, exp.Not):
            return {"T": "F", "F": "T"}.get(self._truth_at(n.this, assign), "N")
        if isinstance(n, (exp.Like, exp.ILike)):
            v = assign[_fingerprint(n.this)]
            return "N" if v is None else ("T" if v == _LIKE_MATCH else "F")
        if type(n).__name__ in _COMPARE_OPS:
            subject = n.this if _literal_value(n.expression) is not None else n.expression
        else:
            subject = n.this
        return self._truth(n, assign[_fingerprint(subject)])

    def compare(self, conds_a: list[Any], conds_b: list[Any], grouped: bool = False,
                never_null: bool = False) -> bool | None:
        """两组条件（各自 AND 连，可以一边为空 = 不筛）留下的取值是不是一样：一样 True，不一样 False，认不出 None。
        never_null：对象一定不为空（表结构里不为空的列），不拿空值比。"""
        parsed = [self.subject(c) for c in conds_a + conds_b]
        if any(p is None for p in parsed) or len({p[0] for p in parsed}) != 1:
            return None
        subject = parsed[0][1]
        literals = [lit for p in parsed for lit in p[2]]
        if len({k for k, _ in literals}) > 1:
            return None
        ranged = any(p[3] for p in parsed)
        from sqlglot import exp

        values = self._pieces(subject, literals, ranged, grouped)
        if not isinstance(_unparen(subject), exp.Count) and not never_null:
            values.append(None)
        for v in values:
            ta = all(self._truth(c, v) == "T" for c in conds_a)
            tb = all(self._truth(c, v) == "T" for c in conds_b)
            if ta != tb:
                return False
        return True


# ---- 一边用到了哪些列（判断「另一边没有」能不能说）

class _Usage:
    """一条语句里每一列（按定义找到实表列，认不出来源的只记列名）用在了哪儿：

    - cond：WHERE、ON、HAVING 里，条件里的子查询（IN、EXISTS、标量）选出来的列；
    - case：CASE / IF / FILTER 里（SUM(CASE WHEN status = 'paid' THEN amount END) 等于先按 status 筛再求和），以及聚合
      的参数里套在别的运算、函数里的列（SUM(status = 'paid')、COUNT(status = 'paid' OR NULL)、COUNT(NULLIF(status,
      'void'))、SUM(amount * (status = 'paid'))、SUM(amount * (1 - is_deleted))：乘上 0、变成空值的行就是被筛掉了，
      和写 CASE 是一回事。认不出是不是在筛，一律当成在筛，只会少说）；
    - agg：光一列做聚合函数、窗口函数的参数（COUNT(x)、SUM(x) 跳过空值，等于先筛掉 x 为空的行）。

    光选出来、分组、排序的列不记。说「另一边没有这个条件」「另一边没有按时间筛选」之前，先看另一边在这一段能影响到的
    地方（整条语句，除了合并里别的支）用没用到这些列。

    temporal：这条语句哪儿可能按时间筛了——在条件里（cond / case）用到可能是时间的列（_timeish_name：paid_at，也包括
    date_key、stat_month 这类认不准的）；WHERE、HAVING、ON 里有日期函数、像日期的常量；任何地方（聚合的参数、CASE、
    FILTER 里也算）有可能是时间窗口的比较（_window_like：20260901 这类整数、'2026-09%' 这类模式）；对同一个对象有
    上下界（BETWEEN、x >= … 和 x < … 都有，见 _bounded_both_ways）。这时不说它「没有按时间筛选」：第 5 轮复验抓到
    SUM(paid_at >= '2026-09-01' AND paid_at < '2026-10-01')、date_key BETWEEN 20260901 AND 20260930 都被说成了没有按时间
    筛选。"""

    def __init__(self, tree: Any) -> None:
        from sqlglot import exp

        self.nodes: dict[tuple[str | None, str], dict[int, str]] = {}
        self.temporal = False
        try:
            self.resolver: _Resolver | None = _Resolver(tree)
        except _Unsupported:
            self.resolver = None
        for col in tree.find_all(exp.Column):
            if col.is_star or not col.name:
                continue
            kind = self._kind(col)
            if kind is None:
                continue
            bases = self._bases(col, 0)
            if isinstance(col.this, exp.Identifier) and col.this.meta.get(_UNSURE):
                bases.append((None, col.name))
            for table, column in bases:
                self.nodes.setdefault(_usage_key(table, column), {})[id(col)] = kind
                if kind != "agg" and (_timeish_name(column) or _timeish_name(col.name)):
                    self.temporal = True
        for node in tree.find_all(exp.Where, exp.Having, exp.Join):
            target = node.args.get("on") if isinstance(node, exp.Join) else node.this
            if target is not None and _datey(target):
                self.temporal = True
        if _window_like(tree) or _bounded_both_ways(tree):
            self.temporal = True

    @staticmethod
    def _owner(node: Any) -> tuple[Any, str | None, bool, bool, bool]:
        """node 属于哪一段 SELECT、在它的哪个子句里、路上有没有 CASE / IF / FILTER、有没有聚合 / 窗口函数，以及在聚合的
        参数里是不是套在别的运算、函数里（不是光一列：括号、DISTINCT、CAST 不算）。"""
        from sqlglot import exp

        case = agg = computed = False
        child, parent = node, node.parent
        while parent is not None:
            if isinstance(parent, (exp.Case, exp.If, exp.Filter)):
                case = True
            if isinstance(parent, (exp.AggFunc, exp.Window)):
                agg = True
            elif not agg and not isinstance(parent, (exp.Paren, exp.Distinct, exp.Cast, exp.TryCast)):
                # 只换类型（CAST）还是那一列；别的运算、函数（比较、NULLIF、乘上标记……）可能就是在筛
                computed = True
            if isinstance(parent, exp.Select):
                return parent, child.arg_key, case, agg, computed
            child, parent = parent, parent.parent
        return None, None, case, agg, computed

    def _kind(self, col: Any) -> str | None:
        select, key, case, agg, computed = self._owner(col)
        if select is None:
            return None
        if key == "expressions":
            if _scalar_context(select) or _condition_query(select):
                return "cond"
            if case or (agg and computed):
                return "case"
            return "agg" if agg else None
        if key in ("group", "order", "distinct"):
            return "case" if case else None
        return "cond"

    def _bases(self, col: Any, depth: int) -> list[tuple[str | None, str]]:
        if depth > 16 or self.resolver is None:
            return [(None, col.name)]
        item = self.resolver.source_of.get(id(col))
        if item is None:
            select = self._owner(col)[0]
            items = _items_of(select) if select is not None else []
            if col.table or len(items) != 1:
                return [(None, col.name)]
            item = items[0]
        return self._from_item(item, col.name, depth)

    def _from_item(self, item: Any, name: str, depth: int) -> list[tuple[str | None, str]]:
        from sqlglot import exp

        body = self.resolver.body(item) if self.resolver is not None else None
        if body is None:
            return [(item.name, name)] if isinstance(item, exp.Table) else [(None, name)]
        leaves = _leaves(body)
        if not leaves or not isinstance(leaves[0], exp.Select) or depth > 16:
            return [(None, name)]
        names = [p.alias_or_name for p in leaves[0].expressions]
        if name not in names:
            sources = _items_of(leaves[0])
            if len(leaves) == 1 and any(isinstance(p, exp.Star) for p in leaves[0].expressions) and len(sources) == 1:
                return self._from_item(sources[0], name, depth + 1)
            return [(None, name)]
        index = names.index(name)
        out: list[tuple[str | None, str]] = []
        for leaf in leaves:
            if not isinstance(leaf, exp.Select) or index >= len(leaf.expressions):
                return [(None, name)]
            expr = leaf.expressions[index].unalias()
            for c in ([expr] if isinstance(expr, exp.Column) else list(expr.find_all(exp.Column))):
                if not c.is_star:
                    out += self._bases(c, depth + 1)
        return out or [(None, name)]

    def touches(self, refs: Iterable[tuple[str, str]], skip: Iterable[Any] = (),
                kinds: tuple[str, ...] = ("cond", "case"), scope: Any = None) -> bool:
        """这些实表列用在了 kinds 里（认不出来源的同名列也算）；skip 里的条件中的那几处不算，scope 所在合并的别的支
        （不碰 scope 的行）不算。"""
        from sqlglot import exp

        skipped = {id(c) for cond in skip for c in cond.find_all(exp.Column)}
        node = scope
        while node is not None and node.parent is not None:
            parent = node.parent
            if isinstance(parent, exp.SetOperation):
                for branch in (parent.this, parent.expression):
                    if branch is not node:
                        skipped |= {id(c) for c in branch.find_all(exp.Column)}
            node = parent
        for table, column in refs:
            for key in (_usage_key(table, column), _usage_key(None, column)):
                if any(i not in skipped and k in kinds for i, k in self.nodes.get(key, {}).items()):
                    return True
        return False

    def touches_name(self, columns: Iterable[str], kinds: tuple[str, ...] = ("cond", "case")) -> bool:
        """不管是哪张表的，叫这几个名字的列用在了 kinds 里（两边换了一张表时用：同一个意思的列在两边各属各的表）。"""
        names = {_usage_key(None, c)[1] for c in columns}
        return any(column in names and any(k in kinds for k in found.values())
                   for (_, column), found in self.nodes.items())


def _usage_key(table: str | None, column: str) -> tuple[str | None, str]:
    """_Usage 记一列用的键：表名、列名都不分（ASCII）大小写。SQLite、MySQL 的列名本来就不分；PostgreSQL 带引号的分，
    这里一律当成同一列，只会多算一处「另一边用到了」，少说一句，不会说错。"""
    return (table.translate(_ASCII_LOWER) if table else None), column.translate(_ASCII_LOWER)


def _kinds_for(conds: list[Any]) -> tuple[str, ...]:
    """说某个条件只在一边有之前，要看另一边哪些地方用到了这些列：条件里有判空（IS NULL / IS NOT NULL）的连聚合的
    参数也看（COUNT(x) 本来就跳过 x 为空的行）。"""
    from sqlglot import exp

    if any(isinstance(n, exp.Is) for c in conds for n in c.walk()):
        return "cond", "case", "agg"
    return "cond", "case"


def _condition_query(select: Any) -> bool:
    """这段查询是条件里的子查询（IN、EXISTS、ANY / ALL）：选出来的列参与比较。"""
    from sqlglot import exp

    node = select
    while isinstance(node.parent, (exp.Subquery, exp.SetOperation)):
        node = node.parent
    parent = node.parent
    return isinstance(parent, (exp.Exists, exp.Any, exp.All)) or (isinstance(parent, exp.In) and
                                                                    node.arg_key == "query")


# ---- 一段 SELECT

@dataclass
class _Rel:
    """一段里的一个来源：alias 是规范别名，name 是实表名（CTE 是规范名，子查询是 sub），kind 是 table / cte / derived，
    shown 是给人看的名字（实表是表名，CTE、子查询是原来起的名字），rejected 表示写的是 LEFT JOIN、被 WHERE 变成了
    内连接。"""

    alias: str
    name: str
    kind: str
    shown: str
    node: Any
    rejected: str | None = None

    @property
    def table(self) -> bool:
        return self.kind == "table"


def _rel_of(item: Any, ctes: Mapping[str, str]) -> _Rel:
    from sqlglot import exp

    alias = item.alias_or_name
    if isinstance(item, exp.Subquery):
        return _Rel(alias, "sub", "derived", item.this.meta.get(_ORIGINAL) or alias, item, item.meta.get(_REJECTED))
    if item.name in ctes:
        return _Rel(alias, item.name, "cte", ctes[item.name], item, item.meta.get(_REJECTED))
    return _Rel(alias, item.name, "table", item.name, item, item.meta.get(_REJECTED))


@dataclass
class _Block:
    """一段 SELECT 拆开来比：开头那组逗号连接的来源（rels）、后面的连接（joins：LEFT JOIN、没写成逗号连接的内连接，
    按先后）、WHERE、GROUP BY、HAVING 的各项，选出来的列，排序和取行。"""

    select: Any
    rels: list[_Rel]
    joins: list[Any]
    where: list[Any]
    group: list[Any]
    having: list[Any]
    exprs: list[Any]
    order: list[Any]
    limit: str | None
    offset: str | None
    distinct: bool
    distinct_on: bool
    windowed: bool
    ctes: dict[str, str]
    deduped: bool = False

    @classmethod
    def of(cls, select: Any, read: str | None, ctes: Mapping[str, str], root: bool = False) -> "_Block | None":
        from sqlglot import exp

        if not isinstance(select, exp.Select) or (select.args.get("with_") is not None and not root):
            return None
        items = _items_of(select)
        if not items or any(not isinstance(i, (exp.Table, exp.Subquery)) for i in items):
            return None
        joins = select.args.get("joins") or []
        n = 1
        for join in joins:
            if _comma(join):
                n += 1
            else:
                break
        where, group, having = select.args.get("where"), select.args.get("group"), select.args.get("having")
        order, limit, offset = select.args.get("order"), select.args.get("limit"), select.args.get("offset")
        distinct = select.args.get("distinct")

        def number(node: Any) -> str | None:
            return node.expression.sql(dialect=read) if node is not None and node.expression is not None else None

        return cls(select, [_rel_of(i, ctes) for i in items[:n]], joins[n - 1:],
                   _conjuncts(where.this) if where is not None else [],
                   list(group.expressions) if group is not None else [],
                   _conjuncts(having.this) if having is not None else [],
                   list(select.expressions), list(order.expressions) if order is not None else [],
                   number(limit), number(offset), distinct is not None,
                   bool(distinct is not None and distinct.args.get("on")),
                   any(isinstance(n, exp.Window) for n in _block_nodes(select)), dict(ctes),
                   bool(group is not None and group.meta.get(_FROM_DISTINCT)))

    def all_rels(self) -> list[_Rel]:
        return self.rels + [_rel_of(j.this, self.ctes) for j in self.joins]

    def rel(self, alias: str) -> _Rel | None:
        for r in self.all_rels():
            if r.alias == alias:
                return r
        return None

    def names(self) -> list[str]:
        return [r.name for r in self.all_rels()]


# ---- 具体说法只在简单形状上说

#: 简单形状的一段 SELECT 能有的子句：别的（QUALIFY、WINDOW、优化器提示、加锁……）一律不算简单
_SIMPLE_ARGS = frozenset({"expressions", "from_", "joins", "where", "group", "having", "order", "limit", "offset",
                          "distinct"})
#: 不分组、去重之后结果只看取值的集合、不看有几行的聚合（DISTINCT 写不写一样）
_DISTINCT_BLIND = frozenset({"min", "max"})


def _simple_block(tree: Any, read: str | None) -> "_Block | None":
    """规范形式是「简单形状」时拆成 _Block，不是返回 None。简单形状：整条语句就是一段 SELECT——没有 CTE、子查询（FROM
    里的、条件里的、SELECT 列表里的都不行）、UNION 这类合并、窗口函数、DISTINCT ON；FROM 里只有实表，连接只有内连接、
    CROSS JOIN、逗号和 LEFT JOIN（RIGHT / FULL JOIN、NATURAL 都不算）；LIMIT / OFFSET 是写死的数，LIMIT 至少是 1
    （LIMIT 0 两边都没有行，什么不同都看不出来）；没有认不准是哪一列的名字（见 _UNSURE）。

    为什么只在简单形状上说：复验在派生表、合并、窗口函数、子查询上一再抓到说出不存在的差异——外层是 COUNT(DISTINCT)、
    MAX、IN 时子查询里的 UNION 对 UNION ALL、去重、取几行都不起作用，CTE 只在 IN 里用到时里面去不去重也不起作用；
    逐个情况补规则补不完。这些一律只给兜底说明。"""
    from sqlglot import exp

    if not isinstance(tree, exp.Select):
        return None
    if any(value not in (None, False, []) and key not in _SIMPLE_ARGS for key, value in tree.args.items()):
        return None
    for node in tree.walk():
        if node is not tree and isinstance(node, (exp.Query, exp.Subquery, exp.Window)):
            return None
        if isinstance(node, exp.Identifier) and node.meta.get(_UNSURE):
            return None
    # SELECT 列表、HAVING、ORDER BY 里 sqlglot 不认识的函数可能是聚合（SQLite 的 TOTAL）：认不清这一段聚不聚合，去重、
    # LEFT JOIN 看不看得见没关联上的行都说不准
    for clause in (*tree.expressions, tree.args.get("having"), tree.args.get("order")):
        if clause is not None and clause.find(exp.Anonymous) is not None:
            return None
    distinct = tree.args.get("distinct")
    if distinct is not None and distinct.args.get("on"):
        return None
    first = tree.args.get("from_")
    if first is None or not _base_table(first.this):
        return None
    for join in tree.args.get("joins") or []:
        side, kind = str(join.side or "").upper(), str(join.kind or "").upper()
        if not _base_table(join.this) or not _plain_join(join) or side not in ("", "LEFT") or \
                kind not in ("", "INNER", "CROSS", "OUTER") or (side == "LEFT" and join.args.get("on") is None):
            return None
    for key, least in (("limit", 1), ("offset", 0)):
        node = tree.args.get(key)
        if node is not None:
            value = _int_value(node.expression) if node.expression is not None else None
            if value is None or value < least:
                return None
    return _Block.of(tree, read, {}, root=True)


def _base_table(item: Any) -> bool:
    from sqlglot import exp

    return isinstance(item, exp.Table) and isinstance(item.this, exp.Identifier)


def _alias_named(root: Any) -> bool:
    """原文里有 WHERE、ON、GROUP BY、HAVING 用了和同一段的列别名同名、不带表名的名字：SQLite 的 WHERE、GROUP BY 都认
    列别名（WHERE d >= '2026-09-01' 里的 d 是 DATE(paid_at) AS d），不认列的那一级规范化把它当成表的一列，说出来的
    「另一边没有这个条件」「按 d 筛时间」就是错的。"""
    from sqlglot import exp

    for select in root.find_all(exp.Select):
        aliases = {p.alias.translate(_ASCII_LOWER) for p in select.expressions if isinstance(p, exp.Alias) and p.alias}
        if not aliases:
            continue
        clauses = [select.args.get(k) for k in ("where", "group", "having")] + \
            [j.args.get("on") for j in select.args.get("joins") or []]
        for clause in clauses:
            if clause is None:
                continue
            for col in clause.find_all(exp.Column):
                if not col.table and not _nested(col, clause) and col.name.translate(_ASCII_LOWER) in aliases:
                    return True
    return False


class _Explainer:
    """在两边的规范形式上找有把握、又确实影响结果的不同，写成具体的中文句子，同时记下是不是所有不同都说到了。

    只在两边都是简单形状时说（见 _simple_block），也只说下面五类；别的不同（算出来的值上换了筛选的值、
    关联条件、分组、HAVING、排序、取几行、选出来的列、聚合的参数……）一律算没说到，交给兜底说明：

    - time：底层表上按哪一列筛时间不同（两边各在一列上按时间筛，列不同，另一边哪儿都没用这一列筛选，时间列不在 LEFT
      JOIN 的可空一侧）；一边按时间筛了、另一边哪儿都没有可能是时间窗口的条件的（见 _Usage.temporal），算多出来的
      筛选，也说成 time。时间条件见 timed（比较、IN、BETWEEN，时间列 LIKE '2026-09%'）。
    - filter：一边多一个只涉及一张底层表的列、和常量比的筛选条件：另一边整条语句（WHERE、ON、HAVING、CASE / FILTER 里，
      聚合的参数里套在别的运算、函数里的，判空、或者条件只筛掉空值时连光一列的聚合参数）对这些列没有任何条件；这一边
      对这些列的条件全是另一边没有的（不是被另一边也有的条件蕴含的，x > 5 之外多写的 x IS NOT NULL 不说）；比的对象就是
      那一列（LOWER(x) <> 'PAID' 这类认不出取值范围、可能恒为真）；按取值逐段算过不是恒为真的（见 _Range.always，一定
      不为空的列不算空值，见 _never_null）；不用到 LEFT JOIN 可空一侧的列（反连接，见 _on_nullable_side）；不是认不准
      的时间窗口（见 _window_like）。
    - filter / time_range（a、b 两边都有）：同一列上的条件两边各写了一套、留下的取值确实不同（等号对 IN、取值集合不同、
      范围的端点不同），说「A 对 X 的条件是 …，B 是 …」，见 _rewrites。
    - aggregate / group_by：选出来的列只差在顶层的聚合函数（同一列做参数，SUM 对 AVG；COUNT(*) 对 SUM(x)）或者去重上
      （COUNT / SUM / AVG 去不去重，COUNT(*) 对 COUNT(DISTINCT x)，SELECT DISTINCT 对不去重），分组一样。参数在组里
      定死的不说（被 WHERE / ON 定成常量、等于分组键、本身是常量、它那张表的键在组里定死了，见 _fixed_in_group）；去重
      不起作用的不说：MIN / MAX；不分组的聚合只出一行；已经按选出来的列分组；只取一行；表结构里的键说明每行本来就不一样
      （见 _maybe_unique）。每组只有一行时 SUM、AVG、MIN、MAX 都是那一行的值，也不说。
    - join / tables：关联的表不同（换了一张表、一边多关联了一张表）或者关联类型不同（同一个条件，LEFT JOIN 对内连接）。
      LEFT JOIN 对内连接要可空一侧在别处（WHERE、HAVING、后面的连接）都没用到、结果看得见没关联上的那些行（不是只取可空
      一侧跳过空值的聚合、FILTER）；一边多出来的 LEFT JOIN 要那张表在 ON 之外没用到、结果怕重复的行；表结构说关联起来
      每行正好一行（外键、键）的不说。原文写的是 RIGHT JOIN、被 WHERE 或者聚合改成内连接的，说法和原文对不上，不说；
      另一边原文也 LEFT JOIN 了这张表、被规范化去掉了的（见 _drop_unused_left_joins），不说「另一边没有」。

    两边的 WHERE、HAVING、ON 里有可能恒为假的条件（常量条件、同一个对象上取不到值的几个条件）时什么都不说：结果都是空的，
    别处的不同看不出来。结果看不见行的（只出一行、SELECT 列表里又没有一项本身就是聚合，见 _rows_visible）不说筛选、
    关联上的不同。说的都是「这一段的这一项两边各写的是什么」；两边给人看的写法一样、和已经说过的一句正好反过来的不说。"""

    def __init__(self, read: str | None, labels: tuple[str, str], names: Mapping[str, str] | None,
                 info: _Schema) -> None:
        self.read = read
        self.la, self.lb = labels
        self.label = _labeler(names)
        self.info = info
        self.items: list[FacetDiff] = []
        self.range = _Range(read)
        self.usage_a: _Usage | None = None
        self.usage_b: _Usage | None = None
        #: 换了一张表（见 _swapped）
        self.swapped = False
        #: 两边的结果看得见各自有哪些行（见 _rows_visible）：看不见时筛选、关联上的不同都说不准
        self.visible = True
        #: 有一句话没说出口（两边给人看的写法一样，或者和前面的话矛盾）：这一处不同就没说到
        self.withheld = False

    def sql(self, node: Any) -> str:
        """给人看的写法：按方言生成；生成的写法读回来不是同一棵树时（SQLite 把 CAST(x AS DATE) 生成 DATE(x)，两者
        在 SQLite 里结果不同）改用 sqlglot 的通用写法，免得给人看的和实际的不是一回事。"""
        import sqlglot

        text = node.sql(dialect=self.read, comments=False)
        try:
            if _fingerprint(sqlglot.parse_one(text, read=self.read)) == _fingerprint(node):
                return text
        except Exception:  # noqa: BLE001 - 片段单独读不回来：用通用写法
            pass
        return node.sql(comments=False)

    @staticmethod
    def fp(node: Any) -> str:
        return _fingerprint(node)

    # ---- 入口：整条语句

    def statement(self, form_a: _Form, form_b: _Form, roots: tuple[Any, Any] = (None, None)) -> bool:
        """比两条语句的规范形式，返回是不是全都说到了。roots 是两边解析出来的原样的树（不认列的那一级要拿它看原文里
        有没有用列别名的条件）。"""
        A, B = _simple_block(form_a.tree, self.read), _simple_block(form_b.tree, self.read)
        if A is None or B is None:
            return False
        if not (form_a.qualified and form_b.qualified) and any(r is None or _alias_named(r) for r in roots):
            return False
        if self._dead(A) or self._dead(B):
            return False
        self.visible = self._rows_visible(A) and self._rows_visible(B)
        self.usage_a, self.usage_b = _Usage(form_a.tree), _Usage(form_b.tree)
        matched = self.relations(A, B)
        if matched is None:
            return False
        covered, mapping, skip_a, skip_b = matched
        covered &= self.conditions(A, B, mapping, skip_a, skip_b)
        covered &= self.outputs(A, B, mapping)
        covered &= self.rest(A, B, mapping)
        return covered and not self.withheld

    # ---- 说法的拼法

    def add(self, aspect: str, a: Any, b: Any, text: str, detail: dict[str, Any] | None = None) -> None:
        """记一句话。两边给人看的写法一样（「A 算的是 1，B 算的是 1」）、和已经说过的一句正好反过来（A 说 X、B 说 Y
        对上 A 说 Y、B 说 X）的不说：读起来自相矛盾，这一处算没说到。"""
        if isinstance(a, str) and isinstance(b, str) and a == b:
            self.withheld = True
            return
        for old in self.items:
            if old.aspect == aspect and old.a == b and old.b == a and (a is not None or b is not None):
                self.items.remove(old)
                self.withheld = True
                return
            if old.text == text:
                return
        self.items.append(FacetDiff(aspect, a, b, text, detail))

    def base_columns(self, node: Any, block: _Block) -> list[tuple[_Rel, Any]] | None:
        """node 只涉及这一段的实表列（同名的表在这一段只出现一次）时，返回各列和它的来源；否则 None。"""
        from sqlglot import exp

        names = block.names()
        out = []
        for col in node.find_all(exp.Column):
            if col.is_star:
                return None
            rel = block.rel(col.table) if col.table else (block.rels[0] if len(block.all_rels()) == 1 else None)
            if rel is None or not rel.table or names.count(rel.name) != 1:
                return None
            out.append((rel, col))
        return out

    def refs(self, node: Any, block: _Block) -> set[tuple[str, str]] | None:
        cols = self.base_columns(node, block)
        return None if cols is None else {(r.name, c.name) for r, c in cols}

    def show(self, node: Any, block: _Block, *, full: bool = False) -> str:
        """给人看的写法：只有一个来源时列不写表名，否则写实表名（规范别名换回表名）；选出来的列去掉规范化起的别名。
        full 时一律写表名。"""
        from sqlglot import exp

        copy = node.unalias().copy() if isinstance(node, exp.Alias) else node.copy()
        single = len(block.all_rels()) == 1 and not full
        for col in copy.find_all(exp.Column):
            written = col.meta.get(_WRITTEN_COLUMN)
            if written is not None:
                # 规范化按关联条件换成了同一组里的另一列（见 _substitute_equal_columns）：给人看原文写的那一列
                col.set("this", exp.to_identifier(written[1]))
                col.set("table", None if single else exp.to_identifier(written[0]))
                continue
            rel = block.rel(col.table) if col.table else None
            if rel is not None:
                col.set("table", None if single else exp.to_identifier(rel.shown))
        return self.sql(copy)

    def show_all(self, conds: list[Any], block: _Block) -> str:
        """几个条件用 AND 连起来给人看：OR 套上括号（按 sqlglot 的结合规则生成）。"""
        from sqlglot import exp

        if len(conds) == 1:
            return self.show(conds[0], block)
        return self.show(exp.and_(*[c.copy() for c in conds], copy=False), block)

    def named(self, table: str, column: str, *, qualified: bool = False) -> str:
        """列带中文名（有的话）：「支付时间 paid_at」。qualified 时写成「表.列」：两边的时间列在不同的表上（A 按
        orders.paid_at、B 按 users.created_at），或者说「另一边没有按时间筛选」的这一边关联了好几张表时，光写 created_at
        分不清是订单的下单时间还是用户的注册时间。"""
        label = self.label((table, column))
        shown = f"{table}.{column}" if qualified else column
        return _say(label, shown) if label else shown

    @staticmethod
    def what(rel: _Rel) -> str:
        return _say(rel.shown, "表")

    @staticmethod
    def mapped(node: Any, mapping: Mapping[str, str]) -> Any:
        """B 的一个节点按来源的对应关系（B 的别名 → A 的别名）改名后的副本：只用来和 A 比是不是同一个写法。"""
        from sqlglot import exp

        if not mapping:
            return node
        copy = node.copy()
        for col in copy.find_all(exp.Column):
            if col.table in mapping:
                col.set("table", exp.to_identifier(mapping[col.table]))
        return copy

    def mfp(self, node: Any, mapping: Mapping[str, str]) -> str:
        return self.fp(self.mapped(node, mapping))

    # ---- 先确认看得出不同：条件不会恒为假

    def _dead(self, block: _Block) -> bool:
        """这一段的条件可能恒为假（结果一定是空的，或者只剩一行全是空值的聚合），两边就算别处不同也看不出来：常量条件
        （没有列、没有聚合的，1 = 0）；同一个对象只和常量比的几个条件按取值逐段算，哪个值都不成立的（x > 5 AND x < 3、
        分组后 COUNT(*) < 0）。几列交错的矛盾（x > y AND y > x）认不出来，见模块说明。"""
        from sqlglot import exp

        groups = [(block.where, False), (block.having, bool(block.group))] + \
            [(_conjuncts(j.args["on"]), False) for j in block.joins if j.args.get("on") is not None]
        never_null = self._never_null(block)
        for conds, grouped in groups:
            by_subject: dict[str, list[Any]] = {}
            for c in conds:
                if c.find(exp.Column) is None and c.find(exp.AggFunc) is None:
                    return True
                found = self.range.subject(c)
                if found is not None:
                    by_subject.setdefault(found[0], []).append(c)
            if any(self.range.satisfiable(group, grouped, fp in never_null) is False
                   for fp, group in by_subject.items()):
                return True
        return False

    def _nullable(self, block: _Block) -> set[str]:
        """外连接可空一侧、而且真的可能整行为空的来源：LEFT JOIN 进来的，后面没有引用它的内连接、WHERE 里也没有把它
        整行为空的行筛掉的条件（见 _rejects）——不然没关联上的行反正留不下来，它的列就和内连接时一样。"""
        from sqlglot import exp

        out = set()
        for i, join in enumerate(block.joins):
            if str(join.side or "").upper() != "LEFT":
                continue
            alias = join.this.alias_or_name
            later = any(not str(k.side or "") and k.args.get("on") is not None and
                        alias in {c.table for c in k.args["on"].find_all(exp.Column)} for k in block.joins[i + 1:])
            if not later and not any(_rejects(c, alias, self.read) for c in block.where):
                out.add(alias)
        return out

    def _never_null(self, block: _Block) -> set[str]:
        """这一段里一定不为空的列（的指纹）：表结构里确定不为空（NOT NULL、主键，见 _not_null_column），又不在真的可能
        整行为空的外连接一侧（见 _nullable）。只拿来少说：恒为真的条件、取不到值的条件多认出几个。"""
        from sqlglot import exp

        nullable = self._nullable(block)
        out = set()
        for col in block.select.find_all(exp.Column):
            cols = self.base_columns(col, block) if col.name and not col.is_star else None
            if cols and cols[0][0].alias not in nullable and \
                    _not_null_column(self.info, cols[0][0].name, col.name, self.read):
                out.add(_fingerprint(col))
        return out

    @staticmethod
    def _top_aggregates(block: _Block) -> list[Any]:
        """SELECT 列表里本身就是一个聚合的项（外面最多套着 ROUND、CAST、给默认值的 COALESCE，见 _unwrap_value），不带
        FILTER、不是窗口函数。COUNT(*) > -1、SUM(x) / COUNT(*) 这类拿聚合再算的不算：前者恒为真，看不出有几行。"""
        from sqlglot import exp

        out = []
        for e in block.exprs:
            node = _unwrap_value(e.unalias())
            if isinstance(node, exp.AggFunc) and not isinstance(node.parent, (exp.Filter, exp.Window)):
                out.append(node)
        return out

    def _rows_visible(self, block: _Block) -> bool:
        """结果看得见这一段筛下来、关联出来的是哪些行：不聚合（行就在结果里）、分组（组在不在看得见），或者不分组的
        聚合里有一项本身就是聚合（见 _top_aggregates）。SELECT COUNT(*) > -1 FROM t 这类只出一行、值又和行无关的，
        多一个条件、少一张表都看不出来。"""
        if not _aggregating(block.select) or block.group:
            return True
        return bool(self._top_aggregates(block))

    # ---- 来源

    @staticmethod
    def _shape(block: _Block) -> tuple[dict[str, tuple[str, str]], list[tuple[str, str, str, str]]]:
        """这一段的来源：开头那组内连接 {别名: (表名, 种类)} 和后面的连接 [(别名, 表名, 种类, ON 的写法)]，种类是 inner /
        left。"""
        prefix = {r.alias: (r.name.translate(_ASCII_LOWER), "inner") for r in block.rels}
        joins = [(j.this.alias_or_name, j.this.name.translate(_ASCII_LOWER),
                  "left" if str(j.side or "").upper() == "LEFT" else "inner",
                  _fingerprint(j.args.get("on")) if j.args.get("on") is not None else "") for j in block.joins]
        return prefix, joins

    def _touched(self, usage: _Usage | None, cols: Iterable[tuple[str, str]],
                 kinds: tuple[str, ...] = ("cond", "case")) -> bool:
        """另一边用没用到这几列（见 _Usage.touches）。换了表的（_swapped）只按列名看：A 的 orders.is_deleted 在 B 那边
        是 orders_archive.is_deleted，按表名看会以为 B 没用到。"""
        if usage is None:
            return True
        if self.swapped:
            return usage.touches_name({c for _, c in cols}, kinds)
        return usage.touches(cols, kinds=kinds)

    def relations(self, A: _Block, B: _Block) -> tuple[bool, dict[str, str], set[str], set[str]] | None:
        """两段读的来源对不对得上。对得上返回 (说到了没有, B 的别名 → A 的别名, A 已经说过的 WHERE 项, B 已经说过的
        WHERE 项)；对不上返回 None（整条只给兜底）。"""
        pa, ja = self._shape(A)
        pb, jb = self._shape(B)
        if pa == pb and ja == jb:
            return True, {}, set(), set()
        if (said := self._left_on_times(A, B, pa, ja, pb, jb)) is not None:
            return said
        if not ja and not jb:
            only_a = [r for r in A.rels if r.alias not in pb]
            only_b = [r for r in B.rels if r.alias not in pa]
            if len(only_a) == 1 and len(only_b) == 1:
                return self._swapped(A, B, only_a[0], only_b[0])
            if len(only_a) == 1 and not only_b:
                return self._extra_inner(A, B, only_a[0], 0)
            if len(only_b) == 1 and not only_a:
                return self._extra_inner(B, A, only_b[0], 1)
            return None
        kinds_a = {**{a: k for a, (_, k) in pa.items()}, **{a: k for a, _, k, _ in ja}}
        kinds_b = {**{a: k for a, (_, k) in pb.items()}, **{a: k for a, _, k, _ in jb}}
        names_a = {**{a: n for a, (n, _) in pa.items()}, **{a: n for a, n, _, _ in ja}}
        names_b = {**{a: n for a, (n, _) in pb.items()}, **{a: n for a, n, _, _ in jb}}
        only_a, only_b = set(kinds_a) - set(kinds_b), set(kinds_b) - set(kinds_a)
        if any(names_a[x] != names_b[x] for x in set(names_a) & set(names_b)):
            return None
        if len(only_a) == 1 and not only_b and kinds_a[next(iter(only_a))] == "left":
            return self._extra_left(A, B, next(iter(only_a)), 0)
        if len(only_b) == 1 and not only_a and kinds_b[next(iter(only_b))] == "left":
            return self._extra_left(B, A, next(iter(only_b)), 1)
        if only_a or only_b:
            return None
        flipped = [x for x in kinds_a if kinds_a[x] != kinds_b[x]]
        if len(flipped) != 1:
            return None
        x = flipped[0]
        return self._left_inner(A, B, x, 0) if kinds_a[x] == "left" else self._left_inner(B, A, x, 1)

    def _left_on_times(self, A: _Block, B: _Block, pa: Any, ja: Any, pb: Any, jb: Any
                       ) -> tuple[bool, dict[str, str], set[str], set[str]] | None:
        """来源两边一样，只差在一个 LEFT JOIN 的 ON 里的时间条件（日期条件写在 LEFT JOIN 的 ON 里）：说「A 用 LEFT JOIN
        关联 X 表时只关联 … 的行，B 没有这个条件」「A 用 LEFT JOIN 关联 X 表时对 paid_at 的条件是 …，B 是 …」「A … 时按
        paid_at 筛时间，B 按 created_at」。说不准的返回 None（照旧整条兜底）。

        说得准要（每一条都是为了不说出不存在的不同）：开头那组来源一样，后面的连接只有这一个的 ON 不同（别名、表、LEFT 都
        一样）；两边 ON 里没配上的条件每一个都是时间条件（timed）、只涉及这张可空一侧的表的列；这张表在 ON 之外只出现在
        结果里（SELECT 列表、分组、排序），没在 WHERE、HAVING、别的连接里用到（用到了就是反连接、或者关联条件的事）；
        SELECT 列表里用到了它的列（不然 ON 里的不同只影响行会不会重复，说不准）；结果看得见行；这张表在这一段只出现一次。
        两边都有、同一列上的，按取值逐段算过留下的取值确实不同（_Range.compare，同 _rewrites）；只有一边有的，不是恒为真的
        （_always）。aspect 是 join：说的是关联条件，dual_check_facts 不会把它当成业务日期写进数据目录。

        只改说明，不碰等价判定：走到这里时两边已经判了不等价（见 explain_sql_diff）。"""
        from sqlglot import exp

        if pa != pb or len(ja) != len(jb) or not self.visible:
            return None
        differing = [i for i, (x, y) in enumerate(zip(ja, jb)) if x != y]
        if len(differing) != 1:
            return None
        i = differing[0]
        if ja[i][:3] != jb[i][:3] or ja[i][2] != "left":
            return None
        alias = ja[i][0]
        join_a, join_b = A.joins[i], B.joins[i]
        rel = A.rel(alias)
        if rel is None or not rel.table or A.names().count(rel.name) != 1 or B.names().count(rel.name) != 1:
            return None
        on_a, on_b = _conjuncts(join_a.args["on"]), _conjuncts(join_b.args["on"])
        only_a, only_b = self._unmatched(on_a, on_b, {})
        keys: set[tuple[str, str]] = set()
        for block, conds in ((A, only_a), (B, only_b)):
            for cond in conds:
                cols = self.base_columns(cond, block)
                found = self.timed(cond, block)
                if found is None or not cols or any(r.alias != alias for r, _ in cols):
                    return None
                keys.add(found)
        for block, join in ((A, join_a), (B, join_b)):
            if self._used_outside(block, alias, join, outputs=False):
                return None
            if not any(col.table == alias for e in block.exprs for col in e.find_all(exp.Column)):
                return None
        what = self.what(rel)
        if only_a and only_b:
            if len(keys) == 1:
                key = next(iter(keys))
                same_a = [c for c in on_a if self.refs(c, A) == {key}]
                same_b = [c for c in on_b if self.refs(c, B) == {key}]
                if self.range.compare(same_a, same_b, never_null=False) is not False:
                    return None
                shown_a, shown_b = self.show_all(_by_bound(same_a), A), self.show_all(_by_bound(same_b), B)
                text = _say(self.la, "用 LEFT JOIN 关联", what, "时对", self.named(*key), "的条件是", shown_a, "，",
                            self.lb, "是", shown_b)
            else:
                cols_a = {self.timed(c, A) for c in only_a}
                cols_b = {self.timed(c, B) for c in only_b}
                if len(cols_a) != 1 or len(cols_b) != 1:
                    return None
                shown_a, shown_b = self.show_all(only_a, A), self.show_all(only_b, B)
                text = _say(self.la, "用 LEFT JOIN 关联", what, "时按", self.named(*next(iter(cols_a))), "筛时间，",
                            self.lb, "按", self.named(*next(iter(cols_b))))
            self.add("join", shown_a, shown_b, text)
            return True, {}, set(), set()
        side, block, conds = (0, A, only_a) if only_a else (1, B, only_b)
        if self._always(conds, block) is not False:
            return None
        shown = self.show_all(_by_bound(conds), block)
        me, them = (self.la, self.lb) if side == 0 else (self.lb, self.la)
        self.add("join", shown if side == 0 else None, shown if side == 1 else None,
                 _say(me, "用 LEFT JOIN 关联", what, "时只关联", shown, "的行，", them, "没有这个条件"))
        return True, {}, set(), set()

    def _swapped(self, A: _Block, B: _Block, x: _Rel, y: _Rel) -> tuple[bool, dict[str, str], set[str], set[str]] | None:
        """换了一张表：开头那组来源只差一张实表（各自只出现一次、名字不只差在大小写上，都没带库名）。换了表的那一边，
        列照它自己的表说。"""
        if not (x.table and y.table) or A.names().count(x.name) != 1 or B.names().count(y.name) != 1 or \
                x.name.translate(_ASCII_LOWER) == y.name.translate(_ASCII_LOWER) or \
                any(r.node.args.get(k) for r in (x, y) for k in ("db", "catalog")):
            return None
        if not self.visible:
            self.swapped = True
            return False, {y.alias: x.alias}, set(), set()
        self.add("tables", x.name, y.name, _say(self.la, "查的是", x.name, "表，", self.lb, "查的是", y.name, "表"))
        self.swapped = True
        return True, {y.alias: x.alias}, set(), set()

    def _extra_inner(self, big: _Block, small: _Block, rel: _Rel, side: int
                     ) -> tuple[bool, dict[str, str], set[str], set[str]] | None:
        """一边多（内）关联了一张表：WHERE 里涉及这张表的条件就是关联的条件（两边都没有别的连接）。内连接既会筛掉关联
        不上的行、又会把关联上好几行的复制，结果一般不同；表结构说每行正好关联上一行（见 _lossless）时不说。"""
        from sqlglot import exp

        if not rel.table or big.names().count(rel.name) != 1:
            return None
        lbig, lsmall = (self.la, self.lb) if side == 0 else (self.lb, self.la)
        conds = [c for c in big.where if rel.alias in {col.table for col in c.find_all(exp.Column)}]
        skip = {self.fp(c) for c in conds}
        skips = (skip, set()) if side == 0 else (set(), skip)
        # 另一边原文也 LEFT JOIN 了这张表、规范化因为结果不怕重复把它去掉了（见 _drop_unused_left_joins）：不能说「另一边
        # 没有」（第 5 轮复验：COUNT(DISTINCT …) … LEFT JOIN refunds 对内连接 refunds，以前说「A 没有」关联 refunds）
        dropped = small.select.meta.get(_DROPPED_JOINS) or []
        if not self.visible or rel.name in dropped or self._lossless(big, rel, conds):
            return False, {}, *skips
        shown = _say("（关联的条件是", self.show_all(conds, big), "）") if conds and \
            all(self.base_columns(c, big) is not None for c in conds) else ""
        what = self.what(rel)
        self.add("join", what if side == 0 else None, what if side == 1 else None,
                 _say(lbig, "关联了", what, shown, "，", lsmall, "没有"))
        return True, {}, *skips

    def _extra_left(self, big: _Block, small: _Block, alias: str, side: int
                    ) -> tuple[bool, dict[str, str], set[str], set[str]] | None:
        """一边多 LEFT JOIN 了一张表，别的来源两边一样：这张表在它自己的 ON 之外哪儿都没用到时，有没有这次关联只差在
        左边的行会不会因为关联上好几行而重复。结果不怕重复（去重、MIN / MAX、去重的聚合）、表结构里这张表的键出现在 ON
        里（可能每行最多关联上一行）时说不准，不说。"""
        pb, jb = self._shape(big)
        ps, js = self._shape(small)
        if pb != ps or [j for j in jb if j[0] != alias] != js:
            return None
        join = next(j for j in big.joins if j.this.alias_or_name == alias)
        rel = big.rel(alias)
        # 另一边原文也 LEFT JOIN 了这张表、规范化因为结果不怕重复把它去掉了（见 _drop_unused_left_joins）：不能说「另一边
        # 没有」
        dropped = small.select.meta.get(_DROPPED_JOINS) or []
        if rel is None or not rel.table or big.names().count(rel.name) != 1 or join.meta.get(_FLIPPED) or \
                rel.name in dropped or self._used_outside(big, alias, join) or not self._dup_visible(big) or \
                self._keyed(rel, _conjuncts(join.args["on"])):
            return False, {}, set(), set()
        lbig, lsmall = (self.la, self.lb) if side == 0 else (self.lb, self.la)
        what = self.what(rel)
        self.add("join", what if side == 0 else None, what if side == 1 else None,
                 _say(lbig, "用 LEFT JOIN 关联", what, "，", lsmall, "没有"))
        return True, {}, set(), set()

    def _left_inner(self, left: _Block, inner: _Block, alias: str, side: int
                    ) -> tuple[bool, dict[str, str], set[str], set[str]] | None:
        """同一张表，left 那边 LEFT JOIN、inner 那边内连接，关联条件一样（left 的 ON 各项就是 inner 那边 ON 和 WHERE 里
        涉及这张表的各项），别的来源两边一样。说「A 用 LEFT JOIN 关联 X，B 用 INNER JOIN」之前确认两种写法结果真的可能
        不同（见 _observes_unmatched）；可空一侧在 WHERE、HAVING、后面的连接里用到了的（后面的内连接引用可空一侧时，没关联
        上的行反正留不下来）、表结构说每行都关联得上的（见 _lossless）、原文写的是 RIGHT JOIN 或者被 WHERE / 聚合改成
        内连接的（说法和原文对不上）都不说。说不了时照样返回对得上（关联条件那几项不再比），这一处算没说到。"""
        from sqlglot import exp

        pl, jl = self._shape(left)
        pi, ji = self._shape(inner)
        if {a: v for a, v in pl.items() if a != alias} != {a: v for a, v in pi.items() if a != alias} or \
                [j for j in jl if j[0] != alias] != [j for j in ji if j[0] != alias]:
            return None
        join = next(j for j in left.joins if j.this.alias_or_name == alias)
        on = {self.fp(c) for c in _conjuncts(join.args["on"])}
        inner_join = next((j for j in inner.joins if j.this.alias_or_name == alias), None)
        in_where = [c for c in inner.where if alias in {col.table for col in c.find_all(exp.Column)}]
        inner_on = [self.fp(c) for c in _conjuncts(inner_join.args["on"])] if inner_join is not None and \
            inner_join.args.get("on") is not None else []
        # inner 那边的 ON 只能是 left 的 ON 里的项；WHERE 里涉及这张表、又不是 left 的 ON 里的项，是另外的筛选条件，
        # 留给 conditions 比
        if not set(inner_on) <= on or not on <= set(inner_on) | {self.fp(c) for c in in_where}:
            return None
        skip = {self.fp(c) for c in in_where if self.fp(c) in on}
        skips = (set(), skip) if side == 0 else (skip, set())
        rel = left.rel(alias)
        marked = any(r.node.meta.get(_REJECTED) or r.node.meta.get(_NULL_BLIND) for b in (left, inner)
                     for r in b.all_rels()) or any(j.meta.get(_FLIPPED) for b in (left, inner) for j in b.joins)
        if rel is None or not rel.table or left.names().count(rel.name) != 1 or marked or \
                self._used_outside(left, alias, join, outputs=False) or not self._observes_unmatched(left, alias) or \
                self._lossless(left, rel, _conjuncts(join.args["on"])):
            return False, {}, *skips
        lleft, linner = (self.la, self.lb) if side == 0 else (self.lb, self.la)
        what = self.what(rel)
        text = _say(lleft, "用 LEFT JOIN 关联", what, "，", linner, "用 INNER JOIN") if side == 0 else \
            _say(linner, "用 INNER JOIN 关联", what, "，", lleft, "用 LEFT JOIN")
        self.add("join", "OUTER JOIN" if side == 0 else "INNER JOIN", "INNER JOIN" if side == 0 else "OUTER JOIN", text)
        return True, {}, *skips

    def _dup_visible(self, block: _Block) -> bool:
        """结果看得见重复的行：不聚合、不去重（重复的行就在结果里；只取第一行的看不出——重复出来的行和原来那行一样）；
        聚合时 SELECT 列表里有一项本身就是不去重的 COUNT / SUM / AVG（见 _top_aggregates：MIN / MAX、去重的聚合、拿聚合
        再算的看不出）。"""
        from sqlglot import exp

        if _dup_insensitive(block.select) or block.distinct or block.deduped:
            return False
        if not _aggregating(block.select):
            return not (block.limit is not None and block.offset is None and
                        (not block.limit.isdigit() or int(block.limit) < 2))
        return any(isinstance(a, (exp.Count, exp.Sum, exp.Avg)) and not isinstance(a.this, exp.Distinct) and
                   not a.args.get("expressions") for a in self._top_aggregates(block))

    @staticmethod
    def _used_outside(block: _Block, alias: str, join: Any, *, outputs: bool = True) -> bool:
        """alias 那张表的列在它自己的 ON 之外用到了（outputs 为假时不看 SELECT 列表、GROUP BY、ORDER BY：可空一侧的列
        选出来、分组、排序都不会把没关联上的行筛掉）；有不带表名的列（分不清是不是这张表的）也算用到了。"""
        from sqlglot import exp

        own = {id(n) for n in join.args["on"].walk()}
        holders = [block.select.args.get(k) for k in ("where", "having")] + \
            [j.args.get("on") for j in block.joins if j is not join]
        if outputs:
            holders += [*block.select.expressions, block.select.args.get("group"), block.select.args.get("order")]
        for holder in holders:
            for col in holder.find_all(exp.Column) if holder is not None else []:
                if id(col) not in own and (col.table == alias or not col.table):
                    return True
        return False

    def _observes_unmatched(self, block: _Block, alias: str) -> bool:
        """LEFT JOIN 比内连接多出来的只是没关联上的那些行（可空一侧整行是空值）：结果看得见它们才可能不同。看得见的：
        分组（只有没关联上的行的组只在 LEFT JOIN 里有；HAVING 用到可空一侧时说不准）；不聚合（多出来的行就在结果里）；
        不分组的聚合里有 COUNT(*)、或者对保留一侧的列做的聚合（不带 FILTER）。只取可空一侧跳过空值的聚合（SUM(r.x)、
        TOTAL、COUNT(r.x)、MAX、FILTER (WHERE r.x > 0) 里的 COUNT(*)）看不见，两种写法结果一样。"""
        from sqlglot import exp

        if _loose_columns(block.select):
            return False
        if block.group:
            return not any(c.table == alias or not c.table for cond in block.having for c in cond.find_all(exp.Column))
        if not _aggregating(block.select):
            return True
        if block.having:
            return False
        for agg in self._top_aggregates(block):
            if agg.args.get("expressions"):
                continue
            arg = agg.this
            if isinstance(arg, exp.Distinct):
                arg = arg.expressions[0] if len(arg.expressions) == 1 else None
            if isinstance(agg, exp.Count) and isinstance(arg, exp.Star):
                return True
            if type(agg).__name__ in _NULL_SKIPPING and isinstance(arg, exp.Column) and arg.table and arg.table != alias:
                return True
        return False

    def _keys(self, rel: _Rel) -> list[list[str]]:
        table = rel.name.translate(_ASCII_LOWER)
        return ([self.info.pk[table]] if self.info.pk.get(table) else []) + self.info.unique.get(table, [])

    def _keyed(self, rel: _Rel, conds: list[Any]) -> bool:
        """表结构里 rel 那张表有键（主键、唯一约束），键的某一列出现在关联条件里：可能每行最多关联上它一行（不管条件写成
        = 还是 IN），说不准会不会重复。只拿来少说：约束说的「唯一」不一定成立，不拿它判等价。"""
        from sqlglot import exp

        keys = {k for key in self._keys(rel) for k in key}
        return any(col.table == rel.alias and col.name.translate(_ASCII_LOWER) in keys
                   for c in conds for col in c.find_all(exp.Column))

    def _lossless(self, block: _Block, rel: _Rel, conds: list[Any]) -> bool:
        """表结构说内连接 rel 每行都关联得上、又最多一行：这一段别的某张表有指向 rel 的外键，关联条件里又用到了 rel
        的键。这时有没有这次内连接、LEFT JOIN 还是内连接结果都一样，不说。只拿来少说：外键在 SQLite 里默认不检查、
        外键列可能为空，不一定真的每行都对得上。"""
        table = rel.name.translate(_ASCII_LOWER)
        if not self._keyed(rel, conds):
            return False
        return any(to == table for r in block.all_rels() if r.alias != rel.alias and r.table
                   for _, to, _ in self.info.fks.get(r.name.translate(_ASCII_LOWER), []))

    # ---- WHERE

    def timed(self, cond: Any, block: _Block) -> tuple[str, str] | None:
        """时间条件：一列（可以套着日期函数、CAST）和常量比较（=、<>、<、<=、>、>=、IN、BETWEEN），列名像时间或者常量
        像日期；或者时间列（列名像时间、套着日期函数）LIKE 一个像日期的模式（SQLite 里日期存成文字，paid_at LIKE
        '2026-09%' 是取月）。返回 (实表, 列)。只认一定是时间的：date_key BETWEEN 20260901 AND 20260930 这类认不准的
        （见 _window_like）不算，调用方只给兜底。"""
        from sqlglot import exp

        like = isinstance(cond, (exp.Like, exp.ILike)) and not cond.args.get("escape")
        if isinstance(cond, exp.In):
            column_side, others = cond.this, list(cond.expressions)
        elif isinstance(cond, exp.Between):
            if cond.args.get("symmetric"):
                return None
            column_side, others = cond.this, [cond.args.get("low"), cond.args.get("high")]
        elif like:
            column_side, others = cond.this, [cond.expression]
        elif not isinstance(cond, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
            return None
        elif _fixed(cond.expression):
            column_side, others = cond.this, [cond.expression]
        elif _fixed(cond.this):
            column_side, others = cond.expression, [cond.this]
        else:
            return None
        if not others or not all(o is not None and _fixed(o) for o in others):
            return None
        cols = self.base_columns(column_side, block)
        if not cols or len({(r.alias, c.name) for r, c in cols}) != 1 or _value_column(column_side) is None:
            return None
        rel, col = cols[0]
        if like:
            # 模式得像日期（去掉两头的 %、_ 之后），对象得是时间列：status LIKE '2026%' 不是时间窗口
            if not _date_like_literal(others[0]) or not (_temporal_name(col.name) or _datey(column_side)):
                return None
            return rel.name, col.name
        if _temporal_name(col.name) or any(_datey(o) for o in others) or _datey(column_side):
            return rel.name, col.name
        return None

    @staticmethod
    def _left_joined(block: _Block) -> set[str]:
        """这一段里 LEFT JOIN 进来的来源（可空一侧）的别名。"""
        return {j.this.alias_or_name for j in block.joins if str(j.side or "").upper() == "LEFT"}

    def _on_nullable_side(self, conds: list[Any], block: _Block) -> bool:
        """几个条件用到了 LEFT JOIN 可空一侧的列：可空一侧的列为空说的是「没关联上」（反连接），不为空的条件又让 LEFT
        JOIN 变得像内连接——都是关联上的事，不是有效行过滤、时间窗口。认不出列的来源时也算用到了（少说）。"""
        from sqlglot import exp

        left = self._left_joined(block)
        if not left:
            return False
        for cond in conds:
            for col in cond.find_all(exp.Column):
                if col.is_star:
                    continue
                cols = self.base_columns(col, block)
                if not cols or cols[0][0].alias in left:
                    return True
        return False

    def _unmatched(self, xs: list[Any], ys: list[Any], mapping: Mapping[str, str]) -> tuple[list[Any], list[Any]]:
        rest = list(ys)
        only_x = []
        for x in xs:
            found = next((y for y in rest if self.mfp(y, mapping) == self.fp(x)), None)
            if found is None:
                only_x.append(x)
            else:
                rest.remove(found)
        return only_x, rest

    def conditions(self, A: _Block, B: _Block, mapping: Mapping[str, str], skip_a: set[str],
                   skip_b: set[str]) -> bool:
        """WHERE：两边没配上的条件里说得准的三类——按哪一列筛时间不同（_times）、同一列上的条件两边各写了一套（_rewrites）、
        一边多出来的筛选条件（_extra_filters）；剩下的算没说到。"""
        xa = [c for c in A.where if self.fp(c) not in skip_a]
        xb = [c for c in B.where if self.fp(c) not in skip_b]
        only_a, only_b = self._unmatched(xa, xb, mapping)
        if not only_a and not only_b:
            return True
        if not self.visible:
            return False
        only_a, only_b, covered = self._times(A, B, only_a, only_b)
        only_a, only_b = self._rewrites(A, B, xa, xb, only_a, only_b, mapping)
        covered &= self._extra_filters(A, B, xa, only_a, 0)
        covered &= self._extra_filters(B, A, xb, only_b, 1)
        return covered

    def _times(self, A: _Block, B: _Block, only_a: list[Any], only_b: list[Any]) -> tuple[list[Any], list[Any], bool]:
        """两边各在一列上按时间筛、列不同：说「A 按 X 筛时间，B 按 Y」。要求：两边整个 WHERE 里的时间条件各只在这一列
        上，这一列上的条件全是另一边没有的（配上了的说明另一边也用这一列筛），另一边哪儿都没用这一列筛选，两边的时间
        条件都不是恒为真的。说到了的从两边拿掉；不是这种情况的原样还回去（一边按时间筛的算多出来的筛选，见
        _extra_filters；同一列上范围不同的说不了，算没说到）。"""
        ta = [(c, t) for c in only_a if (t := self.timed(c, A)) is not None]
        tb = [(c, t) for c in only_b if (t := self.timed(c, B)) is not None]
        cols_a, cols_b = {t for _, t in ta}, {t for _, t in tb}
        if not ta or not tb or cols_a == cols_b:
            return only_a, only_b, True
        if len(cols_a) != 1 or len(cols_b) != 1 or self.usage_a is None or self.usage_b is None:
            return only_a, only_b, False
        (ka,), (kb,) = cols_a, cols_b
        conds_a, conds_b = [c for c, _ in ta], [c for c, _ in tb]
        all_a = {t for c in A.where if (t := self.timed(c, A)) is not None}
        all_b = {t for c in B.where if (t := self.timed(c, B)) is not None}
        on_a = [c for c in A.where if (self.refs(c, A) or set()) & {ka}]
        on_b = [c for c in B.where if (self.refs(c, B) or set()) & {kb}]
        # 时间列在 LEFT JOIN 的可空一侧：条件管的是关联上的那些行，说成「按它筛时间」说不准
        if all_a != cols_a or all_b != cols_b or len(on_a) != len(conds_a) or len(on_b) != len(conds_b) or \
                self._touched(self.usage_b, {ka}) or self._touched(self.usage_a, {kb}) or \
                self._on_nullable_side(conds_a, A) or self._on_nullable_side(conds_b, B) or \
                self._always(conds_a, A) is True or self._always(conds_b, B) is True:
            return only_a, only_b, False
        qualified = not self.swapped and ka[0] != kb[0]
        text = _say(self.la, "按", self.named(*ka, qualified=qualified), "筛时间，", self.lb, "按",
                    self.named(*kb, qualified=qualified))
        if self._time_shape(conds_a) != self._time_shape(conds_b):
            text += _say("（", self.la, "是", self.show_all(_by_bound(conds_a), A), "，", self.lb, "是",
                         self.show_all(_by_bound(conds_b), B), "）")
        detail = {"a": [{"table": ka[0], "column": ka[1]}], "b": [{"table": kb[0], "column": kb[1]}]}
        self.add("time", ka[1], kb[1], text, detail)
        return [c for c in only_a if all(c is not x for x in conds_a)], \
            [c for c in only_b if all(c is not x for x in conds_b)], True

    def _rewrites(self, A: _Block, B: _Block, xa: list[Any], xb: list[Any], only_a: list[Any], only_b: list[Any],
                  mapping: Mapping[str, str]) -> tuple[list[Any], list[Any]]:
        """同一列上的条件两边各写了一套、留下的取值确实不同：说「A 对 X 的条件是 …，B 是 …」——等号对 IN（口径
        status = 'paid'，这次 status IN ('paid', 'refunded')）、取值集合不同、范围的端点不同。两边都只是「等于常量 / 在
        常量列表里」、又有共同的取值时补一句各多了哪几个取值。说到了的从两边拿掉，说不准的原样还回去（交给
        _extra_filters，多半是兜底）。

        说得准要（每一条都是为了不说出不存在的不同）：两边对这一列都有条件、至少一边有没配上的；两边 WHERE 里用到这一列的
        条件都只涉及这一列（不和别的列交错）、比的就是那一列本身（不是 LOWER(x)），按取值逐段算过两边留下的取值不一样
        （_Range.compare：表结构里确定不为空的列不拿空值比；认不出、算出来一样的不说——x >= 100 AND x >= 50 对 x >= 100
        只是冗余的写法）；这一列不在 LEFT JOIN 的可空一侧（那是关联上的事）；两边在 WHERE 之外（ON、HAVING、CASE / FILTER、
        条件里套着的聚合参数）都没用到这一列（SUM(CASE WHEN status = 'paid' …) 配上 WHERE status IN (…) 可能和只筛
        'paid' 一样）；不是认不准的时间窗口（_window_like：date_key BETWEEN 20260901 AND 20260930）；没换表。两边的条件
        都是时间条件（timed）时 aspect 是 time_range，否则是 filter（a、b 两边都有，dual_check_facts 不会把它当成「一边
        多出来的有效行条件」写进数据目录）。

        只改说明，不碰等价判定：走到这里时两边已经判了不等价（见 explain_sql_diff）。"""
        if self.swapped or self.usage_a is None or self.usage_b is None:
            return only_a, only_b
        refs_a = [self.refs(c, A) for c in xa]
        refs_b = [self.refs(c, B) for c in xb]
        if any(r is None for r in refs_a + refs_b):
            # 有认不出用到哪些列的条件：它可能也用到了这一列，说「A 对 X 的条件是 …」就不全
            return only_a, only_b
        keys: list[tuple[str, str]] = []
        for c, block in [(c, A) for c in only_a] + [(c, B) for c in only_b]:
            found = self.refs(c, block)
            if found is not None and len(found) == 1 and next(iter(found)) not in keys:
                keys.append(next(iter(found)))
        for key in keys:
            on_a = [c for c, r in zip(xa, refs_a) if key in r]
            on_b = [c for c, r in zip(xb, refs_b) if key in r]
            pending_a = [c for c in only_a if any(c is x for x in on_a)]
            pending_b = [c for c in only_b if any(c is x for x in on_b)]
            if not on_a or not on_b or not (pending_a or pending_b):
                continue
            if any(r != {key} for r in refs_a if r & {key}) or any(r != {key} for r in refs_b if r & {key}):
                continue
            if self._on_nullable_side(on_a, A) or self._on_nullable_side(on_b, B):
                continue
            mapped_b = [self.mapped(c, mapping) for c in on_b]
            if not self._plain_subjects(on_a + mapped_b):
                continue
            times = {self.timed(c, A) for c in on_a} | {self.timed(c, B) for c in on_b}
            if times != {key} and any(_window_like(c) for c in on_a + on_b):
                continue
            kinds = _kinds_for(on_a + on_b)
            if self.usage_a.touches({key}, skip=on_a, kinds=kinds) or \
                    self.usage_b.touches({key}, skip=on_b, kinds=kinds):
                continue
            never_null = _not_null_column(self.info, key[0], key[1], self.read)
            if self.range.compare(on_a, mapped_b, never_null=never_null) is not False:
                continue
            shown_a, shown_b = self.show_all(_by_bound(on_a), A), self.show_all(_by_bound(on_b), B)
            text = _say(self.la, "对", self.named(*key, qualified=len(A.all_rels()) > 1), "的条件是", shown_a, "，",
                        self.lb, "是", shown_b)
            more = self._members(on_a), self._members(mapped_b)
            # 两边都只是取值集合、又有共同的取值时补一句各多了哪几个（两边的取值毫不相干时前一句已经说全了）
            if more[0] is not None and more[1] is not None and set(more[0]) & set(more[1]):
                extra_a = [shown for value, shown in more[0].items() if value not in more[1]]
                extra_b = [shown for value, shown in more[1].items() if value not in more[0]]
                parts = [_say(label, "多了", "、".join(extra)) for label, extra in ((self.la, extra_a),
                                                                                  (self.lb, extra_b)) if extra]
                if parts:
                    text = _say(text, "（", "，".join(parts), "）")
            self.add("time_range" if times == {key} else "filter", shown_a, shown_b, text,
                     {"columns": [{"table": key[0], "column": key[1]}]})
            only_a = [c for c in only_a if all(c is not x for x in on_a)]
            only_b = [c for c in only_b if all(c is not x for x in on_b)]
        return only_a, only_b

    def _members(self, conds: list[Any]) -> dict[Any, str] | None:
        """几个条件（AND 连）都是「一列等于常量 / 在常量列表里」时，留下的取值 {比较用的值（见 _Range._norm）: 给人看的
        写法}；有别的写法（比大小、不等于、NOT、OR、列表里的 NULL）返回 None。"""
        from sqlglot import exp

        kept: dict[Any, str] | None = None
        for cond in conds:
            cond = _unparen(cond)
            if isinstance(cond, exp.EQ):
                items = [cond.expression] if _literal_value(cond.expression) is not None else [cond.this]
            elif isinstance(cond, exp.In) and cond.expressions and not cond.args.get("query") and \
                    not cond.args.get("unnest") and not cond.args.get("field"):
                items = list(cond.expressions)
            else:
                return None
            values: dict[Any, str] = {}
            for item in items:
                value = _literal_value(item)
                if value is None:
                    return None
                values.setdefault(self.range._norm(value), self.sql(item))
            kept = values if kept is None else {v: shown for v, shown in kept.items() if v in values}
        return kept

    def _extra_filters(self, block: _Block, other: _Block, where: list[Any], rest: list[Any], side: int) -> bool:
        """block 这一边多出来的筛选条件（rest 是它没配上的 WHERE 项）。按用到的列连成一组（x > 5 和 x IS NOT NULL
        是一组）一组一组看，说得准的说「A 只保留 … 的行，B 没有这个条件」「A 排除了 … 的行，B 没有」「A 按 X 筛时间，
        B 没有按时间筛选」，说不准的算没说到。说得准要：这一组全是另一边没有的（有一项配上了，说明另一边对这些列也有
        条件，多出来的可能被它蕴含）；只涉及一张实表的列；另一边整条语句对这些列没有任何条件（见 _Usage：写在聚合的参数
        里的 SUM(status = 'paid')、SUM(amount * (1 - is_deleted)) 也算有）；不是时间条件的，比的对象就是那几列本身
        （LOWER(x)、LENGTH(x) 的取值范围认不出，x <> 'PAID' 可能恒为真）；按取值逐段算过不是恒为真的（见 _always）。

        这几种不说（第 5 轮复验抓到的）：用到 LEFT JOIN 可空一侧的列的（WHERE r.id IS NULL 是反连接，说的是「没关联
        上」；r.order_id IS NULL 和 ON 里的 r.order_id = o.id 一起已经蕴含 r.id IS NULL，说「另一边没有这个条件」把留得
        多的一边说成了严的一边）；可能是时间窗口、又认不准是哪一列的时间的（date_key BETWEEN 20260901 AND 20260930、
        stat_month = 202609，见 _window_like：说成有效行过滤会被写进数据目录）；说「另一边没有按时间筛选」时另一边可能
        按时间筛了（见 _Usage.temporal）。"""
        usage = self.usage_b if side == 0 else self.usage_a
        me, them = (self.la, self.lb) if side == 0 else (self.lb, self.la)
        covered = True
        pending = list(rest)
        while pending:
            group = self._linked(pending[0], where, block)
            pending = [c for c in pending if all(c is not g for g in group)]
            refs = [self.refs(g, block) for g in group]
            if any(all(g is not r for r in rest) for g in group) or any(r is None for r in refs) or usage is None or \
                    self._on_nullable_side(group, block):
                covered = False
                continue
            cols = set().union(*refs)
            times = {t for g in group if (t := self.timed(g, block)) is not None}
            if not times and any(_window_like(g) for g in group):
                # 可能是时间窗口，又认不准是不是时间列：既不说成有效行过滤，也不说成按它筛时间
                covered = False
                continue
            always = self._always(group, block)
            kinds = _kinds_for(group)
            if "agg" not in kinds and self._always(group, block, assume_values=True) is True:
                # 只筛掉了这几列为空的行（x > 5 OR x <= 5）：SUM(x)、MIN(x) 这类本来就跳过空值，另一边聚合的参数用到
                # 这几列时，有没有这个条件结果可能一样
                kinds = ("cond", "case", "agg")
            if len({t for t, _ in cols}) != 1 or self._touched(usage, cols, kinds) or (
                    times and (len(times) != 1 or len(cols) != 1 or usage.temporal or always is True)) or (
                    not times and (not self._plain_subjects(group) or always is not False)):
                covered = False
                continue
            columns = {"columns": [{"table": t, "column": c} for t, c in sorted(cols)]}
            if times:
                (key,) = times
                shown_key = self.named(*key, qualified=not self.swapped and len(block.all_rels()) > 1)
                text = _say(me, "按", shown_key, "筛时间，", them, "没有按时间筛选") if side == 0 else \
                    _say(them, "没有按时间筛选，", me, "按", shown_key, "筛时间")
                self.add("time", key[1] if side == 0 else None, key[1] if side == 1 else None, text,
                         {"a" if side == 0 else "b": [{"table": key[0], "column": key[1]}],
                          "b" if side == 0 else "a": []})
                continue
            shown = self.show_all(group, block)
            excluded = self._excludes(group[0], block) if len(group) == 1 else None
            text = _say(me, "排除了", excluded, "的行，", them, "没有") if excluded else \
                _say(me, "只保留", shown, "的行，", them, "没有这个条件")
            self.add("filter", shown if side == 0 else None, shown if side == 1 else None, text, columns)
        return covered

    def _linked(self, cond: Any, where: list[Any], block: _Block) -> list[Any]:
        """WHERE 里和 cond 用到同一列（连着传过去）的各项，按原来的先后。认不出用到哪些列的只算它自己。"""
        group = [cond]
        cols = self.refs(cond, block)
        if not cols:
            return group
        changed = True
        while changed:
            changed = False
            for c in where:
                if any(c is g for g in group):
                    continue
                theirs = self.refs(c, block)
                if theirs is not None and theirs & cols:
                    group.append(c)
                    cols |= theirs
                    changed = True
        return [c for c in where if any(c is g for g in group)]

    def _plain_subjects(self, conds: list[Any]) -> bool:
        """几个条件里拿来和常量比的对象都是光一列（不是 LOWER(x)、x + 1 这类算出来的值）。"""
        from sqlglot import exp

        subjects = self.range.subjects(conds)
        return subjects is not None and all(isinstance(_unparen(node), exp.Column) for node in subjects.values())

    def _always(self, conds: list[Any], block: _Block, *, assume_values: bool = False) -> bool | None:
        """几个条件（AND 连起来）是不是恒为真：认得出（每个比的都是对象和常量）时按每个对象的各段取值逐个组合算一遍
        （见 _Range.always）；一定不为空的列（见 _never_null）不取空值，assume_values 时每个对象都不取空值（看这几个
        条件是不是只筛掉了空值）。认不出返回 None。"""
        subjects = self.range.subjects(conds)
        if subjects is None:
            return None
        never_null = set(subjects) if assume_values else self._never_null(block) & set(subjects)
        return self.range.always(conds, never_null)

    @staticmethod
    def _time_shape(conds: list[Any]) -> list[str]:
        """时间条件把那一列换成占位符之后的写法：两边只差时间列时一样。"""
        from sqlglot import exp

        out = []
        for c in conds:
            copy = c.copy()
            for col in list(copy.find_all(exp.Column)):
                col.replace(exp.column("_time_"))
            out.append(_fingerprint(copy))
        return sorted(out)

    def _excludes(self, cond: Any, block: _Block) -> str | None:
        """「排除」的说法：排除的是哪些行。<> 常量、NOT IN 常量列表、NOT LIKE 除了等于 / 落在列表里 / 匹配上的行，
        这一列为空的行也筛掉了（拿空值比是空值，WHERE 只留下真的），照实说出来（表结构里确定不为空的列不说）；IS NOT
        NULL 排除的就是为空的行。别的 NOT（NOT (a AND b) 还把 a、b 有一个为空、另一个成立的行也筛掉了）说不清排除了
        哪些，返回 None，调用方改说「只保留 … 的行」。"""
        from sqlglot import exp

        def and_null(subject: Any, shown: str) -> str:
            cols = self.base_columns(subject, block) if isinstance(_unparen(subject), exp.Column) else None
            if cols and all(_not_null_column(self.info, r.name, c.name, self.read) for r, c in cols):
                return shown
            return _say(shown, "的行和", self.show(subject, block), "为空")

        cond = _unparen(cond)
        if isinstance(cond, exp.NEQ):
            left, right = cond.this, cond.expression
            subject = left if _fixed(right) and not _fixed(left) else (
                right if _fixed(left) and not _fixed(right) else None)
            if subject is None or _quantified(left) or _quantified(right):
                return None
            return and_null(subject, self.show(exp.EQ(this=left.copy(), expression=right.copy()), block))
        if isinstance(cond, exp.Not):
            inner = _unparen(cond.this)
            if isinstance(inner, exp.Is) and isinstance(_unparen(inner.expression), exp.Null):
                return _say(self.show(inner.this, block), "为空")
            if isinstance(inner, exp.In) and inner.expressions and not inner.args.get("query") and \
                    all(_fixed(i) and not isinstance(_unparen(i), exp.Null) for i in inner.expressions) and \
                    not _fixed(inner.this):
                return and_null(inner.this, self.show(inner, block))
            if isinstance(inner, (exp.Like, exp.ILike)) and _fixed(inner.expression) and not _fixed(inner.this):
                return and_null(inner.this, self.show(inner, block))
        return None

    # ---- 选出来的列：聚合函数、去重

    def outputs(self, A: _Block, B: _Block, mapping: Mapping[str, str]) -> bool:
        """选出来的列和分组。两边选出来的列一样、只差去重的，说去重（_dedup）；分组一样、只差在几列顶层的聚合上的，
        逐列说（_aggregates）。别的不同算没说到。"""
        ta = [self.fp(e.unalias()) for e in A.exprs]
        tb = [self.mfp(e.unalias(), mapping) for e in B.exprs]
        ga = sorted(self.fp(g) for g in A.group)
        gb = sorted(self.mfp(g, mapping) for g in B.group)
        dedup_a, dedup_b = A.distinct or A.deduped, B.distinct or B.deduped
        if ta == tb and ga == gb and A.distinct == B.distinct:
            return True
        if ta == tb and dedup_a != dedup_b:
            return self._dedup(A, B, ga, gb)
        if ga != gb or A.distinct != B.distinct or dedup_a != dedup_b or len(ta) != len(tb):
            return False
        covered = True
        for x, y, fx, fy in zip(A.exprs, B.exprs, ta, tb):
            if fx != fy and not self._aggregates(x, y, A, B, mapping):
                covered = False
        return covered

    def _dedup(self, A: _Block, B: _Block, ga: list[str], gb: list[str]) -> bool:
        """一边去重（SELECT DISTINCT，规范化写成了按选出来的列 GROUP BY）、另一边不去重，选出来的列一样。去重不起作用的
        不说：不分组的聚合只出一行；已经按选出来的列分组；只取一行；表结构里的键说明每行本来就不一样。"""
        from sqlglot import exp

        side = 0 if (A.distinct or A.deduped) else 1
        D, N = (A, B) if side == 0 else (B, A)
        if D.deduped:
            if N.group or D.distinct:
                return False
        elif ga != gb:
            return False
        if _aggregating(D.select) and not D.deduped:
            if not D.group:
                return False
            outs = {self.fp(e.unalias()) for e in D.exprs}
            if all(self.fp(g) in outs for g in D.group):
                return False
        if D.limit is not None and D.offset is None and (not D.limit.isdigit() or int(D.limit) < 2):
            return False
        chosen = {self._col_key(c, D) for e in D.exprs if isinstance(c := _unparen(e.unalias()), exp.Column)}
        if self._maybe_unique(D, {k for k in chosen if k is not None}):
            return False
        me, them = (self.la, self.lb) if side == 0 else (self.lb, self.la)
        if D.deduped:
            keys = "、".join(self.show(k, D) for k in D.group)
            text = _say(me, "对", keys, "去重，", them, "没有去重")
        else:
            keys = "DISTINCT"
            text = _say(me, "用 SELECT DISTINCT 去掉了重复的行，", them, "没有")
        self.add("group_by", keys if side == 0 else None, keys if side == 1 else None, text)
        return True

    def _aggregates(self, x: Any, y: Any, A: _Block, B: _Block, mapping: Mapping[str, str]) -> bool:
        """同一位置上两边各是一个顶层的聚合（不套在别的表达式里、不带 FILTER）：只说函数不同（同一个参数）和去重不同
        （COUNT / SUM / AVG 去不去重、COUNT(*) 对 COUNT(DISTINCT x)）。COUNT(x) 对 COUNT(*)（只差在 x 为空的行）、参数
        不同的都不说。"""
        from sqlglot import exp

        xu, yu = x.unalias(), y.unalias()
        ga, gb = _aggregate(xu), _aggregate(yu)
        if ga is None or gb is None:
            return False
        (fa, arg_a, da), (fb, arg_b, db) = ga, gb
        # 参数要是光一列，而且在每一组里不是定死的：MIN(5) 和 MAX(5)、WHERE x = 5 之后的 MIN(x) 和 MAX(x)、按 g 分组后的
        # AVG(DISTINCT g) 和 AVG(g)、x 定成 1 时的 SUM(x) 和 COUNT(*) 都一样
        for arg, block in ((arg_a, A), (arg_b, B)):
            if arg is not None and (not isinstance(_unparen(arg), exp.Column) or self.base_columns(arg, block) is None
                                    or self._fixed_in_group(block, _unparen(arg))):
                return False
        same_arg = (arg_a is None and arg_b is None) or (
            arg_a is not None and arg_b is not None and self.fp(arg_a) == self.mfp(arg_b, mapping))
        wa = self.show(arg_a, A) if arg_a is not None else "*"
        wb = self.show(arg_b, B) if arg_b is not None else "*"
        what_a = ("数的是行数",) if fa == "count" and arg_a is None else ("对", wa, _verb(fa, da))
        what_b = ("数的是行数",) if fb == "count" and arg_b is None else ("对", wb, _verb(fb, db))
        if same_arg and arg_a is not None and fa != fb and da == db:
            # 每组只有一行时 SUM、AVG、MIN、MAX 都是那一行的值
            if {fa, fb} <= {"sum", "avg", "min", "max"} and (self._single_rows(A) or self._single_rows(B)):
                return False
            text = _say(self.la, "对", wa, _verb(fa, da) + "，", self.lb, _verb(fb, db))
        elif same_arg and arg_a is not None and fa == fb and da != db:
            block, arg = (A, arg_a) if da else (B, arg_b)
            if fa in _DISTINCT_BLIND or self._unique_value(block, arg):
                return False
            text = _say(self.la, "对", wa, _verb(fa, da) + "，", self.lb, "没去重") if da else \
                _say(self.la, "对", wa, _verb(fa, da) + "时没去重，", self.lb, _verb(fb, db))
        elif fa == fb == "count" and (arg_a is None) != (arg_b is None):
            block, arg, distinct = (B, arg_b, db) if arg_a is None else (A, arg_a, da)
            if not distinct or self._unique_value(block, arg):
                return False
            text = _say(self.la, *what_a, "，", self.lb, *what_b)
        elif fa != fb and (arg_a is None) != (arg_b is None):
            text = _say(self.la, *what_a, "，", self.lb, *what_b)
        else:
            return False
        self.add("aggregate", self.show(x, A), self.show(y, B), text)
        return True

    def _fixed_in_group(self, block: _Block, col: Any) -> bool:
        """col 在每一组里是不是只有一个值：被 WHERE、ON 里的「列 = 常量」定死，或者等于某个分组键（顺着 WHERE、ON 里
        两列相等传过去），或者它那张表的某个键（主键、唯一约束）在组里定死了（同一组里这张表就只有一行的值：按 u.id 分组
        时 u.city 只有一个值，LEFT JOIN、内连接把行复制了也一样，MAX(u.city) 和 MIN(u.city)、AVG(DISTINCT o.amount) 和
        AVG(o.amount) 都一样——第 5 轮复验抓到以前照说不误）。认不出是哪一列的也当成定死了（少说）。

        表结构里的键只拿来少说：类型亲和、COLLATE 让「唯一」不成立时，这里多当成定死了，只会少说一句，不会说错。"""
        from sqlglot import exp

        key = self._col_key(col, block)
        fixed = self._pinned(block) | {self._col_key(_unparen(g), block) for g in block.group
                                       if isinstance(_unparen(g), exp.Column)}
        keys = {r.alias: self._keys(r) for r in block.all_rels() if r.table}
        whole: set[str] = set()
        changed = True
        while changed:
            changed = False
            for c in self._all_conds(block):
                c = _unparen(c)
                if isinstance(c, exp.EQ) and isinstance(_unparen(c.this), exp.Column) and \
                        isinstance(_unparen(c.expression), exp.Column):
                    left, right = self._col_key(_unparen(c.this), block), self._col_key(_unparen(c.expression), block)
                    for x, y in ((left, right), (right, left)):
                        if x is not None and (x in fixed or x[0] in whole) and y is not None and y not in fixed:
                            fixed.add(y)
                            changed = True
            for alias, groups in keys.items():
                if alias not in whole and any(key_cols and all((alias, k) in fixed for k in key_cols)
                                              for key_cols in groups):
                    whole.add(alias)
                    changed = True
        return key is None or key in fixed or key[0] in whole

    # ---- 表结构里的键：只拿来少说

    @staticmethod
    def _all_conds(block: _Block) -> list[Any]:
        """WHERE 和各个连接的 ON 里 AND 连着的各项。LEFT JOIN 的 ON 也算：「列 = 常量」「两列相等」在关联上的行里成立，
        没关联上的行那一侧整行是空值——拿来判断「定死了」「可能唯一」只会多认（少说），不会少认。"""
        return block.where + [p for j in block.joins if j.args.get("on") is not None for p in _conjuncts(j.args["on"])]

    def _pinned(self, block: _Block) -> set[tuple[str, str]]:
        """WHERE、ON 里被「列 = 常量」定死的列。"""
        from sqlglot import exp

        out = set()
        for c in self._all_conds(block):
            c = _unparen(c)
            if isinstance(c, exp.EQ):
                for col, other in ((c.this, c.expression), (c.expression, c.this)):
                    col = _unparen(col)
                    if isinstance(col, exp.Column) and _literal_value(other) is not None:
                        out.add(self._col_key(col, block))
        return {k for k in out if k is not None}

    @staticmethod
    def _col_key(col: Any, block: _Block) -> tuple[str, str] | None:
        rels = block.all_rels()
        alias = col.table or (rels[0].alias if len(rels) == 1 else None)
        return (alias, col.name.translate(_ASCII_LOWER)) if alias else None

    def _maybe_unique(self, block: _Block, chosen: set[tuple[str, str]]) -> bool:
        """表结构里的键说明：chosen 这几列（加上 WHERE 里被常量定死的列）定下来之后，这一段最多只剩一行——每张表都能由
        定下来的列推出它的某个键（主键、唯一约束），等式两边一边定下来另一边也定下来，一张表的键定下来它的列都定下来。
        这时去重、每组只取一个值不起作用，不说。只拿来少说：认不全列（不带表名）时当成可能唯一。没给键时 False。"""
        from sqlglot import exp

        rels = block.all_rels()
        keys = {r.alias: self._keys(r) for r in rels if r.table}
        if not any(keys.values()):
            return False
        if any(c.name and not c.table for c in block.select.find_all(exp.Column)) and len(rels) > 1:
            return True
        known = {k for k in chosen if k is not None} | self._pinned(block)
        pairs = []
        for c in self._all_conds(block):
            c = _unparen(c)
            if isinstance(c, exp.EQ) and isinstance(_unparen(c.this), exp.Column) and \
                    isinstance(_unparen(c.expression), exp.Column):
                left, right = self._col_key(_unparen(c.this), block), self._col_key(_unparen(c.expression), block)
                if left and right:
                    pairs.append((left, right))
        whole: set[str] = set()
        changed = True
        while changed:
            changed = False
            for left, right in pairs:
                for x, y in ((left, right), (right, left)):
                    if (x in known or x[0] in whole) and y not in known:
                        known.add(y)
                        changed = True
            for alias, groups in keys.items():
                if alias not in whole and any(all((alias, k) in known for k in key) for key in groups if key):
                    whole.add(alias)
                    changed = True
        return all(r.alias in whole for r in rels)

    def _unique_value(self, block: _Block, arg: Any) -> bool:
        """每组里 arg 的取值是不是可能各不相同（DISTINCT 不起作用）：arg 是一列、和分组键一起定下来就只剩一行时是；
        不是光一列的、表结构又给了键的，当成可能是（少说）。"""
        from sqlglot import exp

        node = _unparen(arg)
        groups = {self._col_key(_unparen(g), block) for g in block.group if isinstance(_unparen(g), exp.Column)}
        if not isinstance(node, exp.Column):
            return any(self._keys(r) for r in block.all_rels() if r.table)
        return self._maybe_unique(block, groups | {self._col_key(node, block)})

    def _single_rows(self, block: _Block) -> bool:
        """每组最多一行：分组键（和 WHERE 里定死的列）定下来就只剩一行。"""
        from sqlglot import exp

        groups = {self._col_key(_unparen(g), block) for g in block.group if isinstance(_unparen(g), exp.Column)}
        if block.group and len(groups) != len(block.group):
            return any(self._keys(r) for r in block.all_rels() if r.table)
        return self._maybe_unique(block, groups)

    # ---- 别的子句

    def rest(self, A: _Block, B: _Block, mapping: Mapping[str, str]) -> bool:
        """HAVING、排序、取几行：不同就算没说到（这几处的不同不说具体的：恒为真的 HAVING、被 WHERE 定成常量的排序键、
        只出一组时的排序都不影响结果，逐个认不全）。"""
        if sorted(self.fp(c) for c in A.having) != sorted(self.mfp(c, mapping) for c in B.having):
            return False
        if [self.fp(o) for o in A.order] != [self.mfp(o, mapping) for o in B.order]:
            return False
        return A.limit == B.limit and A.offset == B.offset


def _aggregate(node: Any) -> tuple[str, Any, bool] | None:
    """光一个聚合（SUM / AVG / COUNT / MIN / MAX，MIN / MAX 只有一个参数：两个参数的 max(a, b) 是取较大的那个，不是
    聚合）：(函数, 参数（COUNT(*) 是 None）, 是否去重)。"""
    from sqlglot import exp

    kinds = {exp.Sum: "sum", exp.Avg: "avg", exp.Count: "count", exp.Min: "min", exp.Max: "max"}
    func = kinds.get(type(node))
    if func is None or node.args.get("expressions"):
        return None
    arg = node.this
    distinct = isinstance(arg, exp.Distinct)
    if distinct:
        if len(arg.expressions) != 1:
            return None
        arg = arg.expressions[0]
    if isinstance(arg, exp.Star):
        arg = None
    return func, arg, distinct


def _verb(func: str, distinct: bool) -> str:
    return ("去重" if distinct else "") + _VERBS.get(func, f"用 {func.upper()} 汇总")


def _by_bound(conds: list[Any]) -> list[Any]:
    """时间条件给人看时下界在前：>=、> 在前，<、<= 在后，其余照原来的先后。"""
    from sqlglot import exp

    def rank(c: Any) -> int:
        return 0 if isinstance(c, (exp.GTE, exp.GT)) else 1 if isinstance(c, (exp.LT, exp.LTE)) else 2

    return sorted(conds, key=rank)


def _text_diff(a: _Form | None, b: _Form | None, read: str | None) -> tuple[str, ...]:
    """规范化之后两条 SQL 不同的行：difflib 比两边 sqlglot pretty 的输出，「- 」是 A 的、「+ 」是 B 的。只有一边有这一级
    的规范形式时不比（只列一边的行读起来像另一边是空的），返回空的，调用方改给原文的逐行对照（见 _raw_diff）。

    两边按同一套排版写（_diff_lines）：WHERE、ON、HAVING 的合取式每个条件一行。sqlglot 的 pretty 按宽度决定换不换行，
    两个条件放得下就排在一行、三个就拆开，同一个条件在两边落在不同的行上，就被当成「不同的行」（2026-10 验收：两边完全
    相同的日期区间被标成了差异，审核人会以为统计期也不一样）。比的时候不看条件前面的 AND：一边多出来的条件排在最前面
    时，另一边同一个条件只差在前面有没有 AND。列出来的仍是各自原样的行。只动排版，判不判等价、差异说明都不看这里。

    不同的行前面带上它的各级上级行（_ancestors：排在前面、缩进比它浅的最近一行，一层层数到顶格），两边相同的那几行以
    「  」开头列出，只为标出不同的行在哪个子句里——哪个 WHERE、哪个 JOIN 的 ON、哪层子查询。只列不同的行时，只差一个
    关联条件的那一边只剩一行「AND 条件」，看不出是哪个 JOIN 的关联条件、还是筛选条件（2026-10 复核）。上级行在两边只差
    前面有没有 AND 时，列不带 AND 的写法（两边共同的部分）。上级行本身不同的，照样是「- 」「+ 」行。"""
    import difflib

    if a is None or b is None:
        return ()
    la, lb = _diff_lines(a, read), _diff_lines(b, read)
    ka, kb = [_LEADING_AND.sub(r"\1", x) for x in la], [_LEADING_AND.sub(r"\1", x) for x in lb]
    # (标记, A 的行号, B 的行号)：ndiff 按顺序走完两边，「  」两边各进一行，「- 」「+ 」各进自己那边一行，「? 」是行内
    # 提示、不占行
    rows: list[tuple[str, int, int]] = []
    i = j = 0
    for line in difflib.ndiff(ka, kb):
        tag = line[:2]
        if tag == "  ":
            rows.append((tag, i, j))
            i, j = i + 1, j + 1
        elif tag == "- ":
            rows.append((tag, i, -1))
            i += 1
        elif tag == "+ ":
            rows.append((tag, -1, j))
            j += 1
    need_a = _ancestors(la, [i for tag, i, _ in rows if tag == "- "])
    need_b = _ancestors(lb, [j for tag, _, j in rows if tag == "+ "])
    out: list[str] = []
    for tag, i, j in rows:
        if tag == "- ":
            out.append(f"- {la[i]}")
        elif tag == "+ ":
            out.append(f"+ {lb[j]}")
        elif i in need_a or j in need_b:
            out.append(f"  {la[i] if la[i] == lb[j] else ka[i]}")
    out = out[:_MAX_DIFF_LINES]
    # 截到 40 行时末尾剩下的上级行后面已经没有不同的行，不留：上级行只跟着不同的行出现
    while out and out[-1].startswith("  "):
        out.pop()
    return tuple(out)


def _ancestors(lines: list[str], changed: list[int]) -> set[int]:
    """changed 这些行的各级上级行的行号：排在前面、缩进比它浅的最近一行是它的上级，一层层往上数到顶格。sqlglot 的
    pretty 里子句的内容缩在子句名下面（WHERE 下的条件、JOIN … ON 下的关联条件、EXISTS( 下的子查询），所以数出来的就是
    它所在的各层子句。空白行不当上级。"""
    def depth(line: str) -> int:
        return len(line) - len(line.lstrip(" "))

    parent = [-1] * len(lines)
    # 往上数时还可能当上级的行：从上往下缩进一行比一行深，比当前行深或一样深的都不是它的上级
    stack: list[int] = []
    for n, line in enumerate(lines):
        if not line.strip():
            continue
        while stack and depth(lines[stack[-1]]) >= depth(line):
            stack.pop()
        parent[n] = stack[-1] if stack else -1
        stack.append(n)
    out: set[int] = set()
    for n in changed:
        k = parent[n]
        # 已经在里面的，它往上的各级也已经在里面了
        while k >= 0 and k not in out:
            out.add(k)
            k = parent[k]
    return out


def _diff_lines(form: _Form, read: str | None) -> list[str]:
    """逐行对照用的写法：sqlglot pretty，WHERE、HAVING、ON 的合取式每个条件一行，ON 挂在 JOIN 那一行的末尾（见
    _line_generator）。生成不了就退回普通的 pretty，再不行用规范形式的原文。"""
    try:
        text = _line_generator(read).generate(form.tree)
    except Exception:  # noqa: BLE001 - 只是给人看的排版，出错不能连累差异说明
        try:
            text = form.tree.sql(dialect=read, pretty=True, comments=False)
        except Exception:  # noqa: BLE001
            text = form.text
    return [line.rstrip() for line in text.splitlines()]


def _condition_root(node: Any) -> str | None:
    """node 是 WHERE、HAVING 的整个条件（"clause"）、JOIN 的 ON 条件（"on"），还是别处的（None）。"""
    from sqlglot import exp

    parent = node.parent
    if isinstance(parent, (exp.Where, exp.Having)) and node.arg_key == "this":
        return "clause"
    if isinstance(parent, exp.Join) and node.arg_key == "on":
        return "on"
    return None


def _line_generator(read: str | None) -> Any:
    """这种方言的 sqlglot 生成器（pretty、不带注释），只改条件的排法：WHERE、HAVING、ON 的整个条件按 AND 拆开，每个条件
    一行，第二个起前面写 AND（各层子查询里的也一样），ON 挂在 JOIN 那一行的末尾。别的照 sqlglot 原样：条件本身太长时
    照样折行，两边同一个条件折得一样。

    - 条件本身是 OR（XOR）连起来的一律加括号：和别的条件并列时本来就要加，单独一个时不加的话，同一个条件两边写法不同。
      条件外面多余的括号、AND 套 AND 的括号去掉（_conjuncts），不改意思。
    - ON 挂在 JOIN 那一行的末尾（表名、子查询的右括号后面），条件各占一行缩在下面：写在 ON 后面的话，第一个条件和后面
      的差在「ON」「AND」上，换个先后就对不上；缩进比 WHERE 的条件深一级，ON 里和 WHERE 里的同一个条件不会被当成同一
      行。ON 不单独占一行：那样两边 JOIN 的表不同时，两边都有的「ON」被当成相同的行，条件前面就没了 ON（2026-10 复核）。
    """
    from sqlglot import exp
    from sqlglot.dialects.dialect import Dialect

    dialect = Dialect.get_or_raise(read)
    base = dialect.generator_class
    cls = _LINE_GENERATORS.get(base)
    if cls is None:
        class _ConditionLines(base):  # type: ignore[misc, valid-type]
            def sql(self, expression: Any, key: str | None = None, comment: bool = True) -> str:
                root = _condition_root(expression) if key is None and isinstance(expression, exp.Expr) else None
                conds = _conjuncts(expression) if root else []
                if not conds:
                    return super().sql(expression, key, comment)
                lines = [self._one_condition(c) for c in conds]
                text = "\n".join([lines[0], *(f"AND {line}" for line in lines[1:])])
                # ON：join_sql 拼成「ON <条件>」并给条件的各行加一级缩进，开头换行就是条件都在 ON 下面；单独一行的 ON
                # 再由下面的 join_sql 挂到上一行末尾
                return "\n" + self.indent(text) if root == "on" else text

            def join_sql(self, expression: Any) -> str:
                # sqlglot 拼出来是换行、缩进一级、「ON 」再换行（ON 单独一行，条件在下面）：把这个 ON 接到上一行（表名、
                # 子查询的右括号）末尾。只认这一层的：子查询里、条件里套着的 JOIN 都缩得更深，对不上
                pad = " " * self.pad
                return super().join_sql(expression).replace(f"\n{pad}ON \n", " ON\n", 1)

            def _one_condition(self, node: Any) -> str:
                # 只有一个条件时它就是整个条件，再走 self.sql 又会被认成整个条件、绕不出来：这一层直接用 sqlglot 原来的
                # 写法（里面各层子查询的条件照样经过上面的 sql）
                text = super().sql(node)
                if not isinstance(node, exp.Connector):
                    return text
                if "\n" not in text:
                    return f"({text})"
                return f"({self.seg(self.indent(text), sep='')}{self.seg(')', sep='')}"

        cls = _LINE_GENERATORS[base] = _ConditionLines
    return cls(dialect=dialect, pretty=True, comments=False)


def _raw_diff(a: str, b: str) -> tuple[str, ...]:
    """原文的逐行对照（没有规范形式可比时用：解析时会读错、丢掉写法的，见 _lost_in_parsing）：difflib 比两边原文的行
    （去掉行尾空白），「- 」是 A 的、「+ 」是 B 的；比不出不同的行（只差在行内空白）就整条各列一行。"""
    import difflib

    def lines(text: str) -> list[str]:
        return [line.rstrip() for line in text.strip().splitlines() if line.strip()]

    out = [line for line in difflib.ndiff(lines(a), lines(b)) if line[:2] in ("- ", "+ ")]
    return tuple(out[:_MAX_DIFF_LINES]) or (f"- {a.strip()}", f"+ {b.strip()}")


def explain_sql_diff(a: str, b: str, dialect: str | None = None, *, schema: Mapping[str, Any] | None = None,
                     names: Mapping[str, str] | None = None, labels: tuple[str, str] = ("A", "B")) -> SqlDiff:
    """两条 SQL 是不是等价、不是的话差在哪。从不抛异常。

    等价怎么判：两边各自规范化（见 _canonical_forms：认列之后的那一级、不认列的轻量一级、只统一名字的一级），每一步都
    只做保持语义的变换；同一级的两个规范形式完全一样（比语法树，见 _Form.key）才算等价，否则一律不等价，items 一定
    不为空。schema 是数据源的 schema_cache，给了就按它认列、展开 SELECT *（表结构要和库对得上）；里面的约束（主键、
    唯一约束、外键、NOT NULL）不拿来判等价，只拿来让说明少说。原文里有 sqlglot 会读错、会丢掉的写法的（MySQL 字符串
    里的反斜杠转义、1_000、没加括号地混用的比较和按位运算、PostgreSQL 写成符号的 ~ / ~~ / @@ 和 || 混用、前缀的 @、XOR、
    一元加号、0x10、换了名字的类型名，不知道是哪种库时套着的块注释、紧跟着字的 --、双引号括起来的名字……，见
    _lost_in_parsing、_misread）不在语法树上判等价，只给兜底和原文的逐行对照。

    不等价时怎么说：两边的规范形式都是简单形状时（一段 SELECT、实表、普通的连接，见 _simple_block），只把五类有把握、
    真会影响结果的不同写成具体的中文句子（按哪一列筛时间、一边多出来的筛选条件、同一列上的条件改写、顶层的聚合函数和
    去重、关联的表和关联类型，见 _Explainer）；具体说法一条都没有时给一条 aspect="other" 的兜底说明（「两条 SQL 的写法不同，未能归纳成具体
    差异，请对照原文」），只覆盖了一部分时追加一条「另有未能归纳的差异」，fallback 为真；text_diff 给出规范化之后不同
    的行（没有规范形式可比时给原文的逐行对照）。

    labels 是两边的叫法（缺省 A / B，双盲复算传「原流程」「盲算」）；names 是列的中文名（{「表.列」或「列」: 中文名}，
    可以用 catalog_labels 从数据目录取），时间列写成「支付时间 paid_at」（两边的时间列在不同的表上时写「表.列」）。任一边
    解析不了：只给一条 aspect="parse"。
    """
    la, lb = labels
    read = _read(dialect)
    if read == "mysql":
        a, b = (_mysql_dashes(x) if isinstance(x, str) else x for x in (a, b))
    ra, ea = _parse(a, read)
    rb, eb = _parse(b, read)
    if ea or eb:
        if ea and eb:
            text = _say(la, "：", ea, "；", lb, "：", eb, "；没有逐项比较")
        else:
            side, error = (la, ea) if ea else (lb, eb)
            text = _say(side, "：", error or "", "，没有逐项比较")
        return SqlDiff(False, [FacetDiff("parse", ea, eb, text)])
    if a.strip() == b.strip():
        # 一字不差就是同一条语句（带会执行的注释、RANDOM() 这些不在语法树上判等价的写法也一样）
        return SqlDiff(True, level=_PARSED)
    info = _schema_info(schema)
    forms_a: dict[str, _Form] = {}
    forms_b: dict[str, _Form] = {}
    try:
        marked_a = _mark_surfaces(ra, a.strip(), read)
        marked_b = _mark_surfaces(rb, b.strip(), read)
        if read == "sqlite":
            _mark_comma_joins(ra, a.strip(), read)
            _mark_comma_joins(rb, b.strip(), read)
        forms_a = {} if not marked_a or _lost_in_parsing(a.strip(), read, ra) else _canonical_forms(ra, read, info)
        forms_b = {} if not marked_b or _lost_in_parsing(b.strip(), read, rb) else _canonical_forms(rb, read, info)
    except Exception:  # noqa: BLE001 - 规范化本身的缺陷不能连累调用方：当作说不清，记下来修
        logger.warning("SQL 规范化失败", exc_info=True)
        forms_a, forms_b = {}, {}
    for level in (_OPTIMIZER, _LIGHT, _PARSED):
        fa, fb = forms_a.get(level), forms_b.get(level)
        if fa is not None and fb is not None and fa.key == fb.key:
            return SqlDiff(True, level=level)
    level = next((lv for lv in (_OPTIMIZER, _LIGHT) if lv in forms_a and lv in forms_b), None)
    items: list[FacetDiff] = []
    covered = False
    if level is not None:
        explainer = _Explainer(read, (la, lb), names, info)
        try:
            covered = explainer.statement(forms_a[level], forms_b[level], (ra, rb))
            items = explainer.items
        except Exception:  # noqa: BLE001 - 说明本身的缺陷不能连累调用方：当作说不清，记下来修
            logger.warning("SQL 差异说明失败", exc_info=True)
            items, covered = [], False
    shown = level or _PARSED
    if not items:
        items = [FacetDiff("other", None, None, _FALLBACK)]
        fallback = True
    elif not covered:
        items = items + [FacetDiff("other", None, None, _FALLBACK_REST)]
        fallback = True
    else:
        fallback = False
    text_diff = _text_diff(forms_a.get(shown), forms_b.get(shown), read) or _raw_diff(a, b)
    return SqlDiff(False, items, fallback, text_diff, level)


def diff_facets(a: Facets, b: Facets, labels: tuple[str, str] = ("A", "B"), *,
                names: Mapping[str, str] | None = None) -> list[FacetDiff]:
    """两份 facets 记下的原文交给 explain_sql_diff 比，返回它的 items（含兜底条目）：完全一致（规范形式一字不差）
    才返回空列表，不等价时一定不为空。

    labels、names 见 explain_sql_diff。任一边解析不了：只返回一条 parse。不是 facets 算出来的 Facets（没记原文）
    比不了原文：同一个对象才返回空列表，否则返回一条兜底说明（facets 记的结构不全，两份一样不说明 SQL 一样）。
    """
    la, lb = labels
    if a.parse_error or b.parse_error:
        if a.parse_error and b.parse_error:
            text = _say(la, "：", a.parse_error, "；", lb, "：", b.parse_error, "；没有逐项比较")
        else:
            side, error = (la, a.parse_error) if a.parse_error else (lb, b.parse_error)
            text = _say(side, "：", error or "", "，没有逐项比较")
        return [FacetDiff("parse", a.parse_error, b.parse_error, text)]
    if a._origin is None or b._origin is None:
        return [] if a is b else [FacetDiff("other", None, None, _FALLBACK)]
    (sql_a, schema_a), (sql_b, schema_b) = a._origin, b._origin
    return explain_sql_diff(sql_a, sql_b, a.dialect or b.dialect, schema=schema_a or schema_b, names=names,
                            labels=labels).items


# ==========================================================================
# 口径模板的参数绑定与等价证明（docs/design/next-round/02-口径账本.md 做法 1）
# ==========================================================================
#
# 口径账本要回答「这次执行的 SQL 是不是某一版口径的取数」。口径的取数节点里写的是模板（WHERE paid_at >= '{{ input.start }}'），
# 这次执行的是字面 SQL。只比 normalize_sql 的文本会错给信任（status = 'paid' 和 'refunded' 文本相同），只拿模板原文和执行的
# SQL 去比 explain_sql_diff 又永远不等价（参数不同），所以分两步：
#
# 1. 绑定（启发）：模板里每个 {{ input.<字段> }} 按位置换成哨兵（引号里是 '__agl_p<i>__' 这样的字，裸写的是一个不会出现的
#    整数），依次在三级规范形式（optimizer / light / parsed，见 _canonical_forms）上抹掉字面量的值比结构；结构一样就按位置
#    把两边的字面量配起来：含哨兵的口径字面量转成正则取出参数（'{{ input.m }}-01' 这种半截的也行），不含哨兵的必须相等，
#    同一个槽位出现几处要取到同一个值。三级都对不上时退回「同一列、同一运算符的常量」配对（启发式，只用于说明和预填）。
# 2. 证明：一律按绑定用 engine/expressions.render_template（运行时同一个渲染器）渲染口径模板，再交给 explain_sql_diff。等价
#    与否只由这一步判——绑定配错了，渲染出来的 SQL 和执行的不等价，结论只会是「不等价」，不会错给信任。
#
# 规范化里按交换律排序的地方（AND 的各项、IN 列表）排的先后和字面量的值有关，哨兵和真实的值排出来的先后可能不同：那一级就
# 配不上（常量对不上），换下一级；parsed 一级不排序，写法和模板同序时总配得上。
#
# 不可比的（照实给原因，不判等价）：模板里引用了 vars.、nodes.、file_text 这类运行时才有的东西；参数写在名字里（双引号、
# 反引号括起来的标识符）；用了 trim / compact 以外的过滤器。

#: 槽位的写法：input.<字段>。字段名可以是中文
_SLOT_PATH = re.compile(r"^input\.([^\s.\[\]|{}()'\"]+)$")
#: 槽位能带的过滤器：trim（去掉首尾空白）、compact（紧凑 JSON）。别的过滤器（upper、json……）倒推不回参数，不可比
_SLOT_FILTERS = frozenset({"trim", "compact"})
#: 模板里的 {{ … }}：和 engine/expressions 的 _TEMPLATE_RE 同一个写法（非贪婪、跨行）
_SLOT_RE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
#: 引号里的哨兵
_STRING_SENTINEL = "__agl_p{}__"
_STRING_SENTINEL_RE = re.compile(r"__agl_p(\d+)__")
#: 裸写的哨兵：这个整数加上槽位的序号。13 位、不以 0 开头，真实的 SQL 里几乎不会写这么一个数；写了也只是这一处配不上
_NUMBER_SENTINEL = 7_340_193_582_000
#: 配对用的三级规范形式（同 explain_sql_diff 比等价的先后），之后是启发式
BINDING_LEVELS = ("optimizer", "light", "parsed", "heuristic")


@dataclass(frozen=True)
class TemplateSlot:
    """模板里的一处 {{ input.<字段> }}：第几处（index）、字段、过滤器、在哪（quoted 引号里的字、bare 裸写、comment 注释里）。"""

    index: int
    field: str
    filters: tuple[str, ...]
    context: str


@dataclass(frozen=True)
class TemplateBinding:
    """bind_template 的结果。

    comparable 为假时 reason 写不可比的原因（模板引用了运行时的变量、参数写在名字里……），binding 为空。
    binding 是 {字段: 值}：引号里取出的是字，裸写的数按原样（能原样写回的转成 int / float）。unbound 是模板里引用了、却没
    取到值的字段（只出现在注释里、哪一级都配不上）。level 是在哪一级配上的（optimizer / light / parsed / heuristic），都没
    配上为 None；heuristic 只是启发，只给说明和预填用，等价仍由 match_template 渲染之后的 explain_sql_diff 判。"""

    comparable: bool
    binding: dict[str, Any] = field(default_factory=dict)
    unbound: tuple[str, ...] = ()
    level: str | None = None
    reason: str | None = None
    slots: tuple[TemplateSlot, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"comparable": self.comparable, "binding": dict(self.binding), "unbound": list(self.unbound),
                "level": self.level, "reason": self.reason}


@dataclass(frozen=True)
class TemplateMatch:
    """match_template 的结果：执行的 SQL 是不是这份口径模板按某组参数渲染出来的。

    equivalent 只由 explain_sql_diff(执行的 SQL, 按 binding 渲染的口径 SQL) 判。binding / unbound / level 同 TemplateBinding
    （level 是绑定配上的那一级，heuristic 表示参数是按「同一列、同一运算符」猜的）；diff 是那一次 explain_sql_diff 的结果
    （不可比时为 None）；rendered 是按绑定渲染出来的口径 SQL；columns 只在等价时给：按位置的列映射
    [{index, ours, caliber}]（外层别名不计、列的先后计；SELECT * 按表结构展开），两边列数对不上、认不出列名时为 None。"""

    equivalent: bool
    comparable: bool = True
    binding: dict[str, Any] = field(default_factory=dict)
    unbound: tuple[str, ...] = ()
    level: str | None = None
    diff: SqlDiff | None = None
    columns: tuple[dict[str, Any], ...] | None = None
    rendered: str | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"equivalent": self.equivalent, "comparable": self.comparable, "binding": dict(self.binding),
                "unbound": list(self.unbound), "level": self.level,
                "diff": self.diff.as_dict() if self.diff is not None else None,
                "columns": [dict(c) for c in self.columns] if self.columns is not None else None,
                "reason": self.reason}


class _NotComparable(Exception):
    """模板不可比：args[0] 是给人看的原因。"""


def _slot_of(expr: str, index: int, context: str) -> TemplateSlot:
    """一处 {{ … }} 的内容 → 槽位；不是 input.<字段>（可带 trim / compact）的抛 _NotComparable。"""
    from app.engine.expressions import template_call

    parts = [p.strip() for p in expr.split("|")]
    head = parts[0]
    if "(" in head and template_call(head) is not None:
        raise _NotComparable("口径的取数 SQL 读了附件的内容，不能按参数比对")
    if re.match(r"^(vars|nodes|output|loops|usage|messages|last_message)\b", head):
        raise _NotComparable("口径的取数 SQL 引用了运行时才有的变量或节点产出，不能按参数比对")
    found = _SLOT_PATH.match(head)
    if found is None:
        raise _NotComparable("口径的取数 SQL 里有认不出的模板写法，不能按参数比对")
    filters = tuple(p for p in parts[1:] if p)
    if bad := [f for f in filters if f not in _SLOT_FILTERS]:
        raise _NotComparable(f"口径的取数 SQL 里的参数用了过滤器「{bad[0]}」，倒推不出参数，不能按参数比对")
    if context == "name":
        raise _NotComparable("口径的取数 SQL 把参数写在了表名或列名里，不能按参数比对")
    return TemplateSlot(index=index, field=found.group(1), filters=filters, context=context)


def _template_slots(template: str) -> tuple[list[tuple[int, int, TemplateSlot]], str]:
    """模板里每一处 {{ … }}：[(起, 止, 槽位)]，以及按哨兵渲染的 SQL。逐字看引号和注释，定下每一处在哪：单引号里是字，
    双引号、反引号里是名字，-- 和 /* */ 里是注释，别处是裸写。{{ … }} 里面的字不参与判断。"""
    spans: list[tuple[int, int, TemplateSlot]] = []
    out: list[str] = []
    state = "code"
    i, n = 0, len(template)
    while i < n:
        if template.startswith("{{", i):
            m = _SLOT_RE.match(template, i)
            if m is not None:
                context = {"code": "bare", "string": "quoted", "name": "name", "line": "comment",
                           "block": "comment"}[state]
                slot = _slot_of(m.group(1).strip(), len(spans), context)
                spans.append((m.start(), m.end(), slot))
                out.append(_STRING_SENTINEL.format(slot.index) if context == "quoted"
                           else str(_NUMBER_SENTINEL + slot.index) if context == "bare" else "")
                i = m.end()
                continue
        ch = template[i]
        if state == "code":
            if ch == "'":
                state = "string"
            elif ch in "\"`":
                state = "name"
                quote = ch
            elif template.startswith("--", i):
                state = "line"
            elif template.startswith("/*", i):
                state = "block"
        elif state == "string" and ch == "'":
            state = "code"          # 成对的 '' 是转义：关上又马上打开，结果一样
        elif state == "name" and ch == quote:
            state = "code"
        elif state == "line" and ch == "\n":
            state = "code"
        elif state == "block" and template.startswith("*/", i):
            out.append("*/")
            i += 2
            state = "code"
            continue
        out.append(ch)
        i += 1
    return spans, "".join(out)


def _erased(node: Any, literals: list[Any]) -> str:
    """语法树的确定写法（同 _fingerprint），只是字面量一律写成 Literal()、不写值，按写出的先后把字面量节点收进 literals。
    两棵树的这个写法一样，就是只差字面量；literals 的先后一一对应。"""
    from sqlglot import exp

    if isinstance(node, exp.Paren):
        return _erased(node.this, literals)
    if isinstance(node, exp.Literal):
        literals.append(node)
        return "Literal()"
    if isinstance(node, exp.Expr):
        parts = [f"{k}={_erased(v, literals)}" for k, v in sorted(node.args.items())
                 if v is not None and v is not False and v != []]
        mark = node.meta.get("sql_semantics_join") if isinstance(node, exp.Join) else None
        if mark:
            parts.append(f"@join={mark}")
        surface = node.meta.get(_SURFACE)
        if surface:
            parts.append(f"@surface={surface}")
        return f"{type(node).__name__}({','.join(parts)})"
    if isinstance(node, list):
        return "[" + ",".join(_erased(v, literals) for v in node) + "]"
    return repr(node)


def _sql_quoted(value: str) -> str:
    """字面量的值写回单引号里的样子：单引号写两遍。"""
    return value.replace("'", "''")


def _bare_value(text: str) -> Any:
    """裸写的数：能原样写回的转成 int / float（预填参数时是数），写不回原样的（100.50、1e3）留成字。"""
    with contextlib.suppress(ValueError):
        number = int(text)
        if str(number) == text:
            return number
    with contextlib.suppress(ValueError):
        real = float(text)
        if repr(real) == text:
            return real
    return text


def _unfilter(text: str, slot: TemplateSlot) -> tuple[bool, Any]:
    """渲染出来的那一截 → 参数的值：compact 按 JSON 倒推，trim 要求首尾没有空白（否则渲染不回原样）。"""
    value: Any = text
    if "compact" in slot.filters:
        try:
            value = json.loads(text)
        except ValueError:
            return False, None
    if "trim" in slot.filters and isinstance(value, str) and value != value.strip():
        return False, None
    return True, value


class _Pairing:
    """按位置配字面量：模板里每一处槽位取到的值；常量对不上、同一个槽位取到不同的值时 ok 为假。"""

    def __init__(self, slots: dict[int, TemplateSlot]) -> None:
        self.slots = slots
        self.values: dict[str, Any] = {}
        self.ok = True

    def give(self, slot: TemplateSlot, value: Any) -> None:
        if slot.field in self.values and self.values[slot.field] != value:
            self.ok = False
            return
        self.values[slot.field] = value

    def pair(self, ours: Any, theirs: Any) -> None:
        """ours 是执行的 SQL 里的字面量，theirs 是哨兵渲染的口径 SQL 里同一位置的字面量。"""
        from sqlglot import exp

        if not self.ok:
            return
        text = str(theirs.this)
        quoted = [self.slots[int(m.group(1))] for m in _STRING_SENTINEL_RE.finditer(text)
                  if int(m.group(1)) in self.slots] if theirs.is_string else []
        bare = None if theirs.is_string else self._bare_slot(text)
        if bare is not None:
            if not isinstance(ours, exp.Literal):
                self.ok = False
                return
            raw = f"'{_sql_quoted(str(ours.this))}'" if ours.is_string else str(ours.this)
            ok, value = _unfilter(raw, bare)
            if ok:
                self.give(bare, value if ours.is_string or "compact" in bare.filters else _bare_value(raw))
            else:
                self.ok = False
            return
        if not quoted:
            # 口径里写死的常量：两边必须一模一样（字还是数、值）
            if not isinstance(ours, exp.Literal) or ours.is_string != theirs.is_string or str(ours.this) != text:
                self.ok = False
            return
        if not ours.is_string:
            self.ok = False
            return
        pattern = "".join("(.*)" if m else re.escape(piece)
                          for piece, m in _split_sentinels(text))
        found = re.fullmatch(pattern, str(ours.this), re.DOTALL)
        if found is None:
            self.ok = False
            return
        for slot, got in zip(quoted, found.groups()):
            ok, value = _unfilter(_sql_quoted(got), slot)
            if not ok:
                self.ok = False
                return
            self.give(slot, value)

    def _bare_slot(self, text: str) -> TemplateSlot | None:
        with contextlib.suppress(ValueError):
            index = int(text) - _NUMBER_SENTINEL
            slot = self.slots.get(index)
            if slot is not None and slot.context == "bare":
                return slot
        return None


def _split_sentinels(text: str) -> list[tuple[str, bool]]:
    """口径字面量切成 (片段, 是不是哨兵)。"""
    out: list[tuple[str, bool]] = []
    pos = 0
    for m in _STRING_SENTINEL_RE.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], False))
        out.append((m.group(0), True))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], False))
    return out


def _parsed_forms(sql: str, read: str | None, info: _Schema) -> tuple[Any, dict[str, _Form]]:
    """(语法树, 三级规范形式)，做法同 explain_sql_diff：原文里有 sqlglot 会读错、会丢掉的写法时不出规范形式。解析不了的
    语法树为 None。"""
    if read == "mysql":
        sql = _mysql_dashes(sql)
    root, _ = _parse(sql, read)
    if root is None:
        return None, {}
    text = sql.strip()
    try:
        marked = _mark_surfaces(root, text, read)
        if read == "sqlite":
            _mark_comma_joins(root, text, read)
        if not marked or _lost_in_parsing(text, read, root):
            return root, {}
        return root, _canonical_forms(root, read, info)
    except Exception:  # noqa: BLE001 - 规范化本身的缺陷：当作这一条没有规范形式（只会少配上）
        logger.warning("口径模板绑定时规范化失败", exc_info=True)
        return root, {}


def _heuristic_pairs(root: Any) -> dict[tuple[str, str, int], list[Any]]:
    """「列 运算符 常量」的几处：{(列名, 运算, 第几个常量): [常量节点…]}，按出现的先后。BETWEEN 的上下界、IN 列表的每一项
    各算一处。只认一边是光一列（不管表名）、另一边是字面量的。"""
    from sqlglot import exp

    out: dict[tuple[str, str, int], list[Any]] = {}

    def column(node: Any) -> str | None:
        node = _unparen(node)
        return name_key(node.name) if isinstance(node, exp.Column) and node.name else None

    def add(col: str | None, op: str, pos: int, lit: Any) -> None:
        lit = _unparen(lit)
        if col is not None and isinstance(lit, exp.Literal):
            out.setdefault((col, op, pos), []).append(lit)

    for node in root.walk():
        if isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Like, exp.ILike)):
            left, right = node.this, node.expression
            op = type(node).__name__
            add(column(left), op, 0, right)
            flipped = {"GT": "LT", "LT": "GT", "GTE": "LTE", "LTE": "GTE"}.get(op, op)
            add(column(right), flipped, 0, left)
        elif isinstance(node, exp.Between):
            add(column(node.this), "Between", 0, node.args.get("low"))
            add(column(node.this), "Between", 1, node.args.get("high"))
        elif isinstance(node, exp.In) and node.args.get("expressions"):
            for item in node.args["expressions"]:
                add(column(node.this), "In", 0, item)
    return out


def _bindings(template_sql: str, sql: str, read: str | None, info: _Schema
              ) -> Iterator[tuple[str, dict[str, Any], tuple[str, ...], tuple[TemplateSlot, ...]]]:
    """按 BINDING_LEVELS 的先后给出配得上的绑定：(哪一级, {字段: 值}, 没取到值的字段, 槽位)，同样的绑定只给一次。
    模板没有槽位时只给一个空绑定（level 为 parsed：用不着配）。不可比抛 _NotComparable。"""
    spans, sentinel_sql = _template_slots(template_sql)
    slots = tuple(slot for _, _, slot in spans)
    fields = tuple(dict.fromkeys(s.field for s in slots))
    if not slots:
        yield _PARSED, {}, (), slots
        return
    root_t, forms_t = _parsed_forms(sentinel_sql, read, info)
    if root_t is None:
        raise _NotComparable("口径的取数 SQL 代入参数后无法解析，不能按参数比对")
    root_e, forms_e = _parsed_forms(sql, read, info)
    if root_e is None:
        return
    by_index = {s.index: s for s in slots}
    seen: list[dict[str, Any]] = []

    def done(values: dict[str, Any]) -> tuple[str, ...]:
        return tuple(f for f in fields if f not in values)

    for level in (_OPTIMIZER, _LIGHT, _PARSED):
        fe, ft = forms_e.get(level), forms_t.get(level)
        if fe is None or ft is None:
            continue
        lits_e: list[Any] = []
        lits_t: list[Any] = []
        if _erased(fe.tree, lits_e) != _erased(ft.tree, lits_t) or len(lits_e) != len(lits_t):
            continue
        pairing = _Pairing(by_index)
        for ours, theirs in zip(lits_e, lits_t):
            pairing.pair(ours, theirs)
        if pairing.ok and pairing.values not in seen:
            seen.append(dict(pairing.values))
            yield level, dict(pairing.values), done(pairing.values), slots
    # 启发式：同一列、同一运算符的常量按出现的先后配（两边那一处的个数一样才配）；还没配上的槽位再按「同一运算符、两边
    # 都只有这一处」配（按 created_at 筛对按 paid_at 筛：对账时照样要预填日期）。只用于说明和预填
    ours_at, theirs_at = _heuristic_pairs(root_e), _heuristic_pairs(root_t)
    pairing = _Pairing(by_index)
    matched = False

    def slotted(b: Any) -> bool:
        return bool(b.is_string and _STRING_SENTINEL_RE.search(str(b.this))) or \
            (not b.is_string and pairing._bare_slot(str(b.this)) is not None)

    for key, theirs in theirs_at.items():
        ours = ours_at.get(key) or []
        if len(ours) != len(theirs):
            continue
        for a, b in zip(ours, theirs):
            if slotted(b):
                pairing.pair(a, b)
                matched = True
    loose_e: dict[tuple[str, int], list[Any]] = {}
    loose_t: dict[tuple[str, int], list[Any]] = {}
    for at, into in ((ours_at, loose_e), (theirs_at, loose_t)):
        for (_, op, pos), lits in at.items():
            into.setdefault((op, pos), []).extend(lits)
    for key, theirs in loose_t.items():
        ours = loose_e.get(key) or []
        if len(ours) == 1 and len(theirs) == 1 and slotted(theirs[0]):
            free = [s for s in pairing.slots.values() if s.field not in pairing.values]
            before = dict(pairing.values)
            probe = _Pairing(by_index)
            probe.pair(ours[0], theirs[0])
            if probe.ok and probe.values and all(f in {s.field for s in free} for f in probe.values):
                pairing.values.update({k: v for k, v in probe.values.items() if k not in before})
                matched = True
    if matched and pairing.ok and pairing.values and pairing.values not in seen:
        yield "heuristic", dict(pairing.values), done(pairing.values), slots


def _slot_defaults(slots: Any) -> dict[str, Any]:
    """入口字段的缺省值 {字段: 缺省值}：slots 可以是入口节点的 fields（[{name, default, …}]）、{字段: {default…}} 或字段名的
    列表（没有缺省值）。"""
    out: dict[str, Any] = {}
    if isinstance(slots, Mapping):
        items = [{"name": k, **(v if isinstance(v, Mapping) else {})} for k, v in slots.items()]
    else:
        items = [s if isinstance(s, Mapping) else {"name": s} for s in (slots or [])]
    for item in items:
        name = item.get("name")
        if isinstance(name, str) and name and item.get("default") not in (None, ""):
            out[name] = item["default"]
    return out


def render_bound(template_sql: str, binding: Mapping[str, Any], slots: Any = None) -> str:
    """按绑定渲染口径模板（engine/expressions.render_template，运行时同一个渲染器）。没绑到的字段取入口字段的缺省值，同
    入口节点的做法（engine/nodes/io.run_input：值为空时取缺省值）。"""
    from app.engine.expressions import render_template

    values = {**_slot_defaults(slots), **{k: v for k, v in binding.items() if v not in (None, "")}}
    return render_template(template_sql, {"input": values})


def bind_template(template_sql: str, slots: Any, sql: str, dialect: str | None = None, *,
                  schema: Mapping[str, Any] | None = None) -> TemplateBinding:
    """从执行的 SQL 反推口径模板的参数（启发，见本节开头）：第一组配得上的绑定。从不抛异常。

    template_sql 是口径取数节点的 args.sql（只认 {{ input.<字段> }}，可带 trim / compact）；slots 是口径入口节点的字段
    （[{name, default, …}]、{字段: {…}} 或字段名的列表，只用来取缺省值，可以不给）；sql 是这次执行的 SQL；dialect、schema
    同 explain_sql_diff。绑定只是启发：配上了不说明等价，要 match_template 渲染之后交 explain_sql_diff 证明。"""
    read = _read(dialect)
    info = _schema_info(schema)
    try:
        for level, values, unbound, found in _bindings(template_sql or "", sql or "", read, info):
            return TemplateBinding(True, values, unbound, level, None, found)
        spans, _ = _template_slots(template_sql or "")
    except _NotComparable as e:
        return TemplateBinding(False, reason=str(e))
    except Exception:  # noqa: BLE001 - 绑定本身的缺陷不能连累调用方：当作没配上
        logger.warning("口径模板绑定失败", exc_info=True)
        spans = []
    fields = tuple(dict.fromkeys(slot.field for _, _, slot in spans))
    return TemplateBinding(True, {}, fields, None, "参数绑不上：这次的 SQL 和口径的写法对不上", tuple(s for _, _, s in spans))


def _result_columns(sql: str, read: str | None, info: _Schema) -> list[str] | None:
    """一条查询选出来的列名（最外层第一支 SELECT，按位置）：SELECT * 按表结构展开（没给表结构认不出时为 None）；没起
    别名的表达式写成它的 SQL。解析不了为 None。"""
    from sqlglot import exp

    root, _ = _parse(_mysql_dashes(sql) if read == "mysql" else sql, read)
    if root is None:
        return None
    branches = _branches(root)
    if not branches:
        return None
    if any(_is_star(item) for item in branches[0].selects):
        if not info.mapping:
            return None
        try:
            from sqlglot.optimizer.qualify import qualify

            root = qualify(root.copy(), dialect=read, schema=info.mapping, expand_stars=True,
                           validate_qualify_columns=False, quote_identifiers=False, identify=False)
        except Exception:  # noqa: BLE001 - 展开不了 *：认不出列名
            return None
        branches = _branches(root)
        if not branches or any(_is_star(item) for item in branches[0].selects):
            return None
    out = []
    for item in branches[0].selects:
        name = item.alias_or_name if isinstance(item, (exp.Alias, exp.Column)) else ""
        out.append(name or item.sql(dialect=read))
    return out


def match_template(template_sql: str, slots: Any, sql: str, dialect: str | None = None, *,
                   schema: Mapping[str, Any] | None = None, names: Mapping[str, str] | None = None,
                   labels: tuple[str, str] = ("这次", "口径"), columns: Sequence[str] | None = None,
                   deadline: float | None = None) -> TemplateMatch:
    """执行的 SQL 是不是这份口径模板按某组参数渲染出来的（见本节开头）。从不抛异常。

    按 bind_template 同样的先后逐个试配得上的绑定，每一组都按绑定渲染口径模板，交给 explain_sql_diff(sql, 渲染结果,
    schema=…, names=…, labels=…)：等价与否只由它判，第一组等价的就是结论。一组都不等价时给第一组绑定（都没配上时按入口
    字段的缺省值渲染）的那次比较：diff 说差在哪（labels 缺省「这次」「口径」）。schema 是查询当时冻结的表结构，names 是
    列的中文名（catalog_labels）。columns 是这次查询结果的列名（查询快照里记的）：等价时据此按位置给列映射，不给就从 SQL
    里认。deadline 是 time.monotonic() 的时刻：过了就不再试下一组绑定（已经试过的照常给结论）。"""
    import time

    read = _read(dialect)
    info = _schema_info(schema)
    sql = sql or ""
    template_sql = template_sql or ""
    tried: list[tuple[str | None, dict[str, Any], tuple[str, ...], SqlDiff, str]] = []
    try:
        candidates = list(_bindings(template_sql, sql, read, info))
    except _NotComparable as e:
        return TemplateMatch(False, comparable=False, reason=str(e))
    except Exception:  # noqa: BLE001 - 绑定本身的缺陷：当作没配上，照样按缺省值比一次
        logger.warning("口径模板绑定失败", exc_info=True)
        candidates = []
    defaults = _slot_defaults(slots)
    for level, values, unbound, found in candidates:
        if deadline is not None and tried and time.monotonic() > deadline:
            break
        rendered = render_bound(template_sql, values, slots)
        diff = explain_sql_diff(sql, rendered, dialect, schema=schema, names=names, labels=labels)
        if diff.equivalent and _unfilled(found, values, defaults):
            # 有槽位既没取到值、也没有缺省值：渲染出来的是口径实际不会执行的 SQL（裸写的槽位成了空白，sqlglot 照样解析得了
            # EXISTS (SELECT  FROM …)），不能拿它证明等价
            diff = SqlDiff(False, [FacetDiff("other", None, None, _UNFILLED)], True, (f"- {sql.strip()}",
                                                                                     f"+ {rendered.strip()}"))
        if diff.equivalent:
            return TemplateMatch(True, binding=values, unbound=unbound, level=level, diff=diff, rendered=rendered,
                                 columns=_column_map(sql, rendered, read, info, columns))
        tried.append((level, values, unbound, diff, rendered))
    if tried:
        level, values, unbound, diff, rendered = tried[0]
        return TemplateMatch(False, binding=values, unbound=unbound, level=level, diff=diff, rendered=rendered)
    try:
        spans, _ = _template_slots(template_sql)
    except _NotComparable as e:
        return TemplateMatch(False, comparable=False, reason=str(e))
    fields = tuple(dict.fromkeys(slot.field for _, _, slot in spans))
    rendered = render_bound(template_sql, {}, slots)
    diff = explain_sql_diff(sql, rendered, dialect, schema=schema, names=names, labels=labels)
    if diff.equivalent and _unfilled(tuple(slot for _, _, slot in spans), {}, defaults):
        diff = SqlDiff(False, [FacetDiff("other", None, None, _UNFILLED)], True,
                       (f"- {sql.strip()}", f"+ {rendered.strip()}"))
    if diff.equivalent:
        # 口径的参数只出现在注释里、或者缺省值正好就是这次的：渲染出来就是等价的
        return TemplateMatch(True, unbound=fields, diff=diff, rendered=rendered,
                             columns=_column_map(sql, rendered, read, info, columns))
    return TemplateMatch(False, unbound=fields, diff=diff, rendered=rendered,
                         reason="参数绑不上：这次的 SQL 和口径的写法对不上" if fields else None)


#: 有槽位没取到值、又没有缺省值时的说法
_UNFILLED = "口径的参数有的没能从这次的 SQL 里取到，又没有缺省值，无法按口径渲染出可比的 SQL"


def _unfilled(slots: Sequence[TemplateSlot], values: Mapping[str, Any], defaults: Mapping[str, Any]) -> bool:
    """有不在注释里的槽位既没取到值、也没有缺省值。引号里取到空字（''）的算取到了：口径按空字渲染就是这一句。"""
    return any(slot.context != "comment" and values.get(slot.field) is None and slot.field not in defaults
               for slot in slots)


def _column_map(sql: str, rendered: str, read: str | None, info: _Schema,
                columns: Sequence[str] | None) -> tuple[dict[str, Any], ...] | None:
    """等价时的列映射：按位置（已实测：外层别名不计、列的先后计）。两边列数对不上、认不出列名时为 None。"""
    ours = [str(c) for c in columns] if columns is not None else _result_columns(sql, read, info)
    theirs = _result_columns(rendered, read, info)
    if ours is None or theirs is None or len(ours) != len(theirs):
        return None
    return tuple({"index": i, "ours": a, "caliber": b} for i, (a, b) in enumerate(zip(ours, theirs)))


# ==========================================================================
# 按引用的列投影（next-round 口径账本：整题等价按答案引用的那几列比）
# ==========================================================================
#
# 真实的助手写 SQL 总会多选几列（订单数、日期常量、MIN / MAX、COALESCE……），报告只引用其中一列；口径的取数也只选它的
# 指标要的那几列。拿整条 SQL 去比永远不等价，「上周的销售额是多少」就永远只标相关口径。这里把最外层 SELECT 列表里没被
# 引用、去掉也不改变被引用那几列取值的选择项去掉（需要时也调换留下的几项的先后），得到一条改写后的 SQL，交给调用方再走
# 现有的绑定和判等价（match_template → explain_sql_diff）：投影本身不判等价、不给信任，判等价的那一步一点不放宽。
#
# 只在确定去掉不影响时才投影，不然照实给不投影的原因（Projection.refused），调用方照旧整条比：
# - 整条语句就是一段 SELECT（可以带 WITH）：UNION 这类合并、外面套着括号的不投影——各支的列按位置合并，去掉一支的一列
#   就错位了；
# - SELECT 里只有 WITH、选择列表、FROM、JOIN、WHERE、GROUP BY、HAVING、QUALIFY、WINDOW、ORDER BY、LIMIT、OFFSET；
#   DISTINCT / DISTINCT ON（按整行去重，去掉一列行数就可能变）和别的修饰（提示、INTO、加锁、采样、LATERAL VIEW……）
#   不投影；
# - 选择列表里没有 * / t.*（展开之后按位置对不准）；给了查询结果的列数（width）时要和选择列表的项数一样：被引用的列按
#   位置对上选择项；
# - ORDER BY / GROUP BY 里没有按位置引用的（ORDER BY 2：去掉、调换之后指到别的列）；
# - 有 LIMIT / OFFSET 时只投影不分组的聚合（只出一行，取哪一行和选出来的列无关）：别的「取前几行」有并列时取到哪几行
#   可能跟着选择列表变；
# - 去掉的每一项：不含认不准会不会改变行数的函数（UNNEST / EXPLODE / generate_series 这类展开成多行的、sqlglot 不认识的
#   函数）；它的输出名（别名，没有别名的是列名）在整条语句别处（同一段的 WHERE、GROUP BY、HAVING、QUALIFY、ORDER BY、
#   窗口、别的选择项、子查询）一次都没有被不带表名地写到——SQLite 的 WHERE、GROUP BY 都认列别名，和表列同名的也算，
#   宁可不投影；不是常量的话，同样的写法没有出现在 GROUP BY、HAVING、ORDER BY、QUALIFY、窗口里（「出现在 GROUP BY 里」
#   「被 HAVING 用到」都在这一条）；
# - 去掉之后这一段聚不聚合不变：没有 GROUP BY、HAVING 时去掉的不能是仅有的聚合（剩下的列就不止一行了）；留下的列里没有
#   既不在聚合里、也不是分组键的列（SQLite 取 MAX 那一行的值、MySQL 取任意一行：去掉别的聚合可能换了取值的那一行）；
# - 调换先后时，留下的各项不按名字写到别的选择项的输出名（DuckDB 这类认「前面的列别名」）。
#
# 改写在原文上做：按方言切词、按括号深度认出最外层 SELECT 列表每一项在原文里的起止，只把留下的几项按新的先后原样拼回去，
# 选择列表前后的原文一字不动（explain_sql_diff 的保真守卫照样看得到原文里的写法）。拼完重新解析，语法树要和「原来的树换成
# 留下的几项」一模一样，不然不投影。口径模板（{{ input.x }}）按 _template_slots 的哨兵渲染之后认起止，再按位置换回模板
# 的原文；MySQL 原文里有被当成两个减号的 --（_mysql_dashes 会改写的）不投影：改写后位置对不上原文。

#: 投影的那一段能有的子句
_PROJECTABLE_ARGS = frozenset({"with_", "expressions", "from_", "joins", "where", "group", "having", "qualify",
                               "windows", "order", "limit", "offset"})
#: 去掉的选择项里不许有的、可能把一行展开成多行的函数（sqlglot 的类名；表函数的基类 UDTF、不认识的函数另认）
_ROW_CHANGING = frozenset({"Explode", "Posexplode", "ExplodeOuter", "PosexplodeOuter", "Unnest", "Inline", "Stack",
                           "ExplodingGenerateSeries", "GenerateSeries"})
#: 最外层 SELECT 列表到这些词就结束了
_LIST_ENDS = _CLAUSE_ENDS | {"FROM", "INTO"}
_OPENS = frozenset({"L_PAREN", "L_BRACKET", "L_BRACE"})
_CLOSES = frozenset({"R_PAREN", "R_BRACKET", "R_BRACE"})

#: 不投影的原因（只进比对的说明和测试，不判等价）
_PROJ_UNPARSED = "SQL 无法解析，不投影"
_PROJ_SETOP = "不是一段 SELECT（UNION 这类合并、外面套着括号），不投影"
_PROJ_DISTINCT = "有 DISTINCT，去掉一列行数就可能变，不投影"
_PROJ_CLAUSE = "SELECT 里有投影认不准的子句（{clause}），不投影"
_PROJ_STAR = "选择列表里有 *，按位置对不准，不投影"
_PROJ_WIDTH = "查询结果的列数（{width}）和选择列表的项数（{items}）对不上，不投影"
_PROJ_KEEP = "要留下的列不对（空的、重复的、超出选择列表），不投影"
_PROJ_ORDINAL = "ORDER BY / GROUP BY 按位置引用了选择项，去掉、调换之后会指到别的列，不投影"
_PROJ_LIMIT = "有 LIMIT / OFFSET，取哪几行可能跟着选择列表变，不投影"
_PROJ_ROWS = "去掉的「{item}」里有可能改变行数的函数，不投影"
_PROJ_NAMED = "去掉的「{item}」在别处被按名字写到了，不投影"
_PROJ_USED = "去掉的「{item}」同样的写法出现在 GROUP BY、HAVING、ORDER BY、QUALIFY 或窗口里，不投影"
_PROJ_AGGREGATE = "去掉的是仅有的聚合，剩下的列就不止一行了，不投影"
_PROJ_LOOSE = "留下的列里有既不在聚合里、也不是分组键的列，去掉别的列可能换了取值的那一行，不投影"
_PROJ_REORDER = "留下的「{item}」按名字写到了别的选择项，不调换先后"
_PROJ_TEXT = "认不出选择列表在原文里的起止，不投影"
_PROJ_DASHES = "原文里有 MySQL 当成两个减号的 --，不投影"


@dataclass(frozen=True)
class Projection:
    """project_select / project_template 的结果。

    sql 是投影后的 SQL（模板投影之后仍是模板，{{ input.x }} 原样留着）；kept 是留下的选择项在原来的选择列表里的位置
    （按投影后的先后）；dropped 是去掉了几项；names 是留下的各项的输出名（别名、列名，别的写成它的 SQL，同
    _result_columns）。refused 不为空时不投影：sql 为 None，写为什么。要留下的就是全部、先后也没变时原样交回（dropped
    为 0）。"""

    sql: str | None
    kept: tuple[int, ...] = ()
    dropped: int = 0
    names: tuple[str, ...] = ()
    refused: str | None = None


class _NotProjectable(Exception):
    """不投影：args[0] 是原因。"""


def _output_name(item: Any, read: str | None) -> str:
    from sqlglot import exp

    name = item.alias_or_name if isinstance(item, (exp.Alias, exp.Column)) else ""
    return name or item.sql(dialect=read)


def _name_of(item: Any) -> str:
    """选择项按名字被引用时的那个名字（别名、列名；小写）。表达式、常量没有。"""
    from sqlglot import exp

    return (item.alias_or_name if isinstance(item, (exp.Alias, exp.Column)) else "").translate(_ASCII_LOWER)


def _ordinal(node: Any) -> bool:
    """ORDER BY / GROUP BY 的一项是按位置引用（一个整数常量）。"""
    from sqlglot import exp

    inner = _unparen(node.this if isinstance(node, exp.Ordered) else node)
    return isinstance(inner, exp.Literal) and not inner.is_string


def _projection_problem(root: Any, keep: Sequence[int], read: str | None) -> str | None:
    """按 keep 投影这一段会不会改变留下的那几列的取值（见本节开头）：会、或者认不准时交回原因，确定不会时为 None。"""
    from sqlglot import exp

    if not isinstance(root, exp.Select):
        return _PROJ_SETOP
    if root.args.get("distinct") is not None:
        return _PROJ_DISTINCT
    extra = sorted(k for k, v in root.args.items() if v not in (None, False, []) and k not in _PROJECTABLE_ARGS)
    if extra:
        return _PROJ_CLAUSE.format(clause=extra[0])
    items = list(root.expressions)
    if any(_is_star(item) for item in items):
        return _PROJ_STAR
    if not keep or len(set(keep)) != len(keep) or any(not isinstance(k, int) or not 0 <= k < len(items) for k in keep):
        return _PROJ_KEEP
    group, order = root.args.get("group"), root.args.get("order")
    for clause in (group, order):
        if clause is not None and any(_ordinal(e) for e in clause.expressions):
            return _PROJ_ORDINAL
    if (root.args.get("limit") is not None or root.args.get("offset") is not None) and \
            not (group is None and _aggregating(root)):
        return _PROJ_LIMIT
    kept = set(keep)
    removed = [i for i in range(len(items)) if i not in kept]
    holders = [c for c in (group, root.args.get("having"), order, root.args.get("qualify")) if c is not None]
    holders += [w for w in root.args.get("windows") or [] if isinstance(w, exp.Expr)]
    holders += [w for k in keep for w in items[k].find_all(exp.Window)]
    used = {_fingerprint(n) for holder in holders for n in holder.walk()}
    for i in removed:
        item = items[i]
        shown = item.sql(dialect=read, comments=False)[:60]
        if any(isinstance(n, (exp.UDTF, exp.Anonymous)) or type(n).__name__ in _ROW_CHANGING for n in item.walk()):
            return _PROJ_ROWS.format(item=shown)
        name = _name_of(item)
        if name:
            inside = {id(n) for n in item.walk()}
            if any(isinstance(n, exp.Column) and not n.table and id(n) not in inside
                   and n.name.translate(_ASCII_LOWER) == name for n in root.walk()):
                return _PROJ_NAMED.format(item=shown)
        body = item.unalias()
        if not _constant(body) and _fingerprint(body) in used:
            return _PROJ_USED.format(item=shown)
    projected = root.copy()
    projected.set("expressions", [items[k].copy() for k in keep])
    if group is None and root.args.get("having") is None and _aggregating(root) != _aggregating(projected):
        return _PROJ_AGGREGATE
    if _loose_columns(projected):
        return _PROJ_LOOSE
    if list(keep) != sorted(keep):
        names = {_name_of(item) for item in items} - {""}
        for k in keep:
            others = names - {_name_of(items[k])}
            if any(isinstance(n, exp.Column) and not n.table and n.name.translate(_ASCII_LOWER) in others
                   for n in items[k].walk()):
                return _PROJ_REORDER.format(item=items[k].sql(dialect=read, comments=False)[:60])
    return None


def _list_spans(text: str, read: str | None) -> list[tuple[int, int]] | None:
    """最外层 SELECT 列表每一项在原文里的起止（含两头）：按方言切词、按括号深度，深度 0 的 SELECT 之后、深度 0 的子句
    开头的词（FROM、WHERE……）之前，深度 0 的逗号分开。深度 0 的 SELECT 不止一个、括号对不上、有空项的为 None。"""
    try:
        tokens = _tokens(text, read)
    except Exception:  # noqa: BLE001 - 切词都不行：认不出
        return None
    depth = 0
    state = 0       # 0：还没到最外层的 SELECT；1：在选择列表里；2：选择列表之后
    spans: list[tuple[int, int]] = []
    start: int | None = None
    end = 0
    for token in tokens:
        kind = token.token_type.name
        if state == 1 and depth == 0 and (kind == "COMMA" or kind in _LIST_ENDS):
            if start is None:
                return None
            spans.append((start, end))
            start = None
            if kind != "COMMA":
                state = 2
            continue
        if kind in _OPENS:
            depth += 1
        elif kind in _CLOSES:
            depth -= 1
            if depth < 0:
                return None
        elif depth == 0 and kind == "SELECT":
            if state != 0:
                return None
            state = 1
            continue
        if state == 1:
            start = token.start if start is None else start
            end = token.end
    if state == 1:
        if start is None:
            return None
        spans.append((start, end))
    return spans or None


def _cut(text: str, spans: Sequence[tuple[int, int]], keep: Sequence[int]) -> str:
    """把选择列表换成留下的几项（按 keep 的先后，原文照抄、逗号隔开）：选择列表前后的原文一字不动。"""
    head, tail = text[:spans[0][0]], text[spans[-1][1] + 1:]
    return head + ", ".join(text[spans[k][0]:spans[k][1] + 1] for k in keep) + tail


def _plan_projection(text: str, keep: Sequence[int], read: str | None, width: int | None
                     ) -> tuple[Any, list[tuple[int, int]] | None, Any]:
    """(原文的语法树, 选择列表各项的起止, 投影后应有的语法树)。原样（keep 就是全部、先后不变）时后两样为 None。不投影抛
    _NotProjectable。"""
    if read == "mysql" and _mysql_dashes(text) != text:
        raise _NotProjectable(_PROJ_DASHES)
    root, _ = _parse(text, read)
    if root is None:
        raise _NotProjectable(_PROJ_UNPARSED)
    from sqlglot import exp

    items = list(root.expressions) if isinstance(root, exp.Select) else []
    if width is not None and isinstance(root, exp.Select) and len(items) != width:
        raise _NotProjectable(_PROJ_WIDTH.format(width=width, items=len(items)))
    if isinstance(root, exp.Select) and list(keep) == list(range(len(items))):
        return root, None, None
    why = _projection_problem(root, keep, read)
    if why:
        raise _NotProjectable(why)
    spans = _list_spans(text, read)
    if spans is None or len(spans) != len(items):
        raise _NotProjectable(_PROJ_TEXT)
    expected = root.copy()
    expected.set("expressions", [items[k].copy() for k in keep])
    return root, spans, expected


def _same_tree(text: str, read: str | None, expected: Any) -> bool:
    root, _ = _parse(text, read)
    return root is not None and _fingerprint(root) == _fingerprint(expected)


def project_select(sql: str, keep: Sequence[int], dialect: str | None = None, *,
                   width: int | None = None) -> Projection:
    """按引用的列投影一条查询（见本节开头）：最外层选择列表只留 keep 那几项（位置从 0 数，按 keep 的先后）。从不抛异常。

    width 是查询结果的列数（查询快照里记的）：给了就要和选择列表的项数一样，被引用的列才按位置对得上选择项。不投影时
    refused 写为什么，调用方照旧整条比。投影出来的 SQL 不判等价：交给 match_template / explain_sql_diff 判。"""
    read = _read(dialect)
    text = (sql or "").strip()
    keep = tuple(keep)
    try:
        root, spans, expected = _plan_projection(text, keep, read, width)
        items = list(root.expressions)
        names = tuple(_output_name(items[k], read) for k in keep)
        if spans is None:
            return Projection(text, keep, 0, names)
        out = _cut(text, spans, keep)
        if not _same_tree(out, read, expected):
            raise _NotProjectable(_PROJ_TEXT)
        return Projection(out, keep, len(items) - len(keep), names)
    except _NotProjectable as e:
        return Projection(None, keep, refused=str(e))
    except Exception:  # noqa: BLE001 - 投影本身的缺陷不能连累调用方：当作不投影，照旧整条比
        logger.warning("SQL 投影失败", exc_info=True)
        return Projection(None, keep, refused=_PROJ_TEXT)


def _template_marks(template: str, spans: Sequence[tuple[int, int, TemplateSlot]]) -> list[tuple[int, int, int, int]]:
    """模板里每一处槽位在哨兵渲染的 SQL 里、在模板原文里的起止：[(哨兵起, 哨兵止, 模板起, 模板止)]（止不含）。槽位之外
    两边逐字相同（_template_slots 只换槽位）。"""
    out: list[tuple[int, int, int, int]] = []
    shift = 0
    for start, end, slot in spans:
        size = len(_STRING_SENTINEL.format(slot.index)) if slot.context == "quoted" else \
            len(str(_NUMBER_SENTINEL + slot.index)) if slot.context == "bare" else 0
        out.append((start - shift, start - shift + size, start, end))
        shift += (end - start) - size
    return out


def _to_template(pos: int, marks: Sequence[tuple[int, int, int, int]], *, last: bool) -> int | None:
    """哨兵 SQL 里的一个位置 → 模板原文里的位置。落在哨兵中间的认不出（选择项的起止只会在一个词的两头）。last：这是一项
    的最后一个字（含），落在哨兵最后一个字上时对到槽位的最后一个字。"""
    delta = 0
    for s0, s1, t0, t1 in marks:
        if pos < s0:
            break
        if pos < s1:
            if not last and pos == s0:
                return t0
            if last and pos == s1 - 1:
                return t1 - 1
            return None
        delta = t1 - s1
    return pos + delta


def project_template(template_sql: str, keep: Sequence[int], dialect: str | None = None) -> Projection:
    """按口径卡读到的列投影口径的取数模板（{{ input.<字段> }} 原样留着，见本节开头）。规则同 project_select，在哨兵渲染
    的 SQL 上判；起止按位置换回模板原文。从不抛异常。"""
    read = _read(dialect)
    template = (template_sql or "").strip()
    keep = tuple(keep)
    try:
        slots, sentinel = _template_slots(template)
        root, spans, expected = _plan_projection(sentinel, keep, read, None)
        items = list(root.expressions)
        names = tuple(_output_name(items[k], read) for k in keep)
        if spans is None:
            return Projection(template, keep, 0, names)
        if not _same_tree(_cut(sentinel, spans, keep), read, expected):
            raise _NotProjectable(_PROJ_TEXT)
        marks = _template_marks(template, slots)
        mapped = [(_to_template(s, marks, last=False), _to_template(e, marks, last=True)) for s, e in spans]
        if any(s is None or e is None for s, e in mapped):
            raise _NotProjectable(_PROJ_TEXT)
        out = _cut(template, mapped, keep)  # type: ignore[arg-type]
        # 换回模板之后再按哨兵渲染一遍：结构（字面量只看位置，槽位的编号会因为去掉了几处而变）要和投影后的一样
        _, again = _template_slots(out)
        root_again, _ = _parse(again, read)
        lits_a: list[Any] = []
        lits_b: list[Any] = []
        if root_again is None or _erased(root_again, lits_a) != _erased(expected, lits_b) or len(lits_a) != len(lits_b):
            raise _NotProjectable(_PROJ_TEXT)
        return Projection(out, keep, len(items) - len(keep), names)
    except _NotComparable as e:
        return Projection(None, keep, refused=str(e))
    except _NotProjectable as e:
        return Projection(None, keep, refused=str(e))
    except Exception:  # noqa: BLE001 - 同 project_select
        logger.warning("口径模板投影失败", exc_info=True)
        return Projection(None, keep, refused=_PROJ_TEXT)


def output_names(sql: str, dialect: str | None = None, *, template: bool = False) -> list[str] | None:
    """最外层选择列表各项的输出名（按位置；同 _result_columns，不展开 *：有 * 的为 None）。template 为真时 sql 是口径
    模板，按哨兵渲染之后认。解析不了、不是一段 SELECT 的为 None。"""
    from sqlglot import exp

    read = _read(dialect)
    text = (sql or "").strip()
    try:
        if template:
            _, text = _template_slots(text)
        root, _ = _parse(_mysql_dashes(text) if read == "mysql" else text, read)
    except _NotComparable:
        return None
    if not isinstance(root, exp.Select) or any(_is_star(i) for i in root.expressions):
        return None
    return [_output_name(i, read) for i in root.expressions]


# ==========================================================================
# 同一个形状（认证队列的「确定性重复」：条件的先后不算）
# ==========================================================================
#
# normalize_sql 的规范文本把字面量换成「?」，但条件照原文的先后：WHERE a = 1 AND b = 2 和 WHERE b = 2 AND a = 1 文本不同，
# 认证队列就拆成两组。这里在不认列的那一级规范形式（_light，和判等价同一套只保持语义的改写）上，把字面量抹成只看类型的
# 占位，再把交换律成立的地方按抹掉之后的写法重排一遍：AND / OR 的各项、IN 列表、= / <> / + / * 的两边、比大小的两边（换边
# 时大于小于跟着换）。规范形式里本来的排序看字面量的值（同一个形状换了参数，条件的先后可能就变了），所以要重排。重排时
# 抹掉之后一样的几项按原值排：同一组里字面量的位置对得上。认不出的（解析不了、sqlglot 会读错的写法、规范化不了的结构）
# 为 None，调用方照旧按 normalize_sql 的文本分组：不同就是不同，只会分得细，不会把不同的并成一组。

#: 抹掉字面量之前给每个字面量记的编号（对回 normalize_sql 的字面量用）
_LIT_TAG = "sql_semantics_literal"
#: 对 normalize_sql 的先后时，数字字面量换成的标记：这个整数加编号（同 _NUMBER_SENTINEL，真实 SQL 里几乎不会写）
_LIT_NUMBER = 9_173_402_581_000
_LIT_STRING = "__agl_l{}__"
_LIT_STRING_RE = re.compile(r"^'__agl_l(\d+)__'$")
#: 交换律成立、两边可以对调的运算
_SYMMETRIC = ("EQ", "NEQ", "Add", "Mul", "NullSafeEQ")


@dataclass(frozen=True)
class SqlShape:
    """sql_shape 的结果。key 一样就是同一个形状（只差字面量的值、条件的先后、交换律成立处两边的先后）；literals 是按规范
    形式里的先后排的字面量（SQL 写法，字符串带引号），同一个形状的几条按位置对得上；positions[i] 是第 i 个在
    normalize_sql(sql).literals 里的位置（规范化时新算出来的字面量对不上，为 None）。"""

    key: str
    literals: tuple[str, ...]
    positions: tuple[int | None, ...]


def _normalized_order(root: Any, read: str | None) -> list[int] | None:
    """normalize_sql 的字面量（按它重新生成的文本里的先后）各是哪一个字面量节点（_LIT_TAG 的编号）：把字面量换成带编号的
    标记，同样地重新生成、切词，按先后读出编号。对不上（个数不同、有认不出的）为 None。"""
    from sqlglot import exp
    from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

    marked = normalize_identifiers(root.copy(), dialect=read)
    for lit in marked.find_all(exp.Literal):
        tag = lit.meta.get(_LIT_TAG)
        if tag is None:
            return None
        lit.set("this", _LIT_STRING.format(tag) if lit.is_string else str(_LIT_NUMBER + tag))
    text = marked.sql(dialect=read, comments=False)
    out: list[int] = []
    for token in _tokens(text, read):
        if not _is_literal(token):
            continue
        raw = text[token.start:token.end + 1]
        found = _LIT_STRING_RE.match(raw)
        if found:
            out.append(int(found.group(1)))
            continue
        try:
            out.append(int(raw) - _LIT_NUMBER)
        except ValueError:
            return None
    return out


def _shape_print(node: Any, read: str | None) -> tuple[str, list[Any]]:
    """规范形式的确定写法（同 _erased：字面量只写类型、不写值，按写出的先后收进列表），交换律成立的地方按抹掉之后的写法
    重排（抹掉之后一样的几项按原值排）。"""
    from sqlglot import exp

    def value_key(lits: list[Any]) -> tuple[str, ...]:
        return tuple(lit.sql(dialect=read) for lit in lits)

    def ordered(parts: list[tuple[str, list[Any]]]) -> list[tuple[str, list[Any]]]:
        return sorted(parts, key=lambda p: (p[0], value_key(p[1])))

    if isinstance(node, exp.Paren):
        return _shape_print(node.this, read)
    if isinstance(node, exp.Literal):
        return f"Literal({'s' if node.is_string else 'n'})", [node]
    if isinstance(node, (exp.And, exp.Or)):
        parts = ordered([_shape_print(p, read) for p in _flat(node, type(node))])
        return (f"{type(node).__name__}[{','.join(p[0] for p in parts)}]",
                [lit for _, lits in parts for lit in lits])
    if isinstance(node, exp.Expr) and type(node).__name__ in _FLIP and not _quantified(node.this) \
            and not _quantified(node.expression):
        left, right = _shape_print(node.this, read), _shape_print(node.expression, read)
        name = type(node).__name__
        if (_fixed(node.this) and not _fixed(node.expression)) or (
                _fixed(node.this) == _fixed(node.expression) and (left[0], value_key(left[1])) > (right[0], value_key(right[1]))):
            left, right, name = right, left, _FLIP[name]
        return f"{name}({left[0]},{right[0]})", left[1] + right[1]
    if isinstance(node, exp.Expr) and type(node).__name__ in _SYMMETRIC:
        parts = ordered([_shape_print(node.this, read), _shape_print(node.expression, read)])
        return f"{type(node).__name__}({','.join(p[0] for p in parts)})", parts[0][1] + parts[1][1]
    if isinstance(node, exp.In) and node.args.get("expressions"):
        head = _shape_print(node.this, read)
        parts = ordered([_shape_print(e, read) for e in node.expressions])
        rest = [f"{k}={_shape_print(v, read)[0]}" for k, v in sorted(node.args.items())
                if k not in ("this", "expressions") and v is not None and v is not False and v != []]
        return (f"In({head[0]};[{','.join(p[0] for p in parts)}];{','.join(rest)})",
                head[1] + [lit for _, lits in parts for lit in lits])
    if isinstance(node, exp.Expr):
        pieces: list[str] = []
        lits: list[Any] = []
        for k, v in sorted(node.args.items()):
            if v is None or v is False or v == []:
                continue
            text, found = _shape_print(v, read)
            pieces.append(f"{k}={text}")
            lits += found
        mark = node.meta.get("sql_semantics_join") if isinstance(node, exp.Join) else None
        if mark:
            pieces.append(f"@join={mark}")
        surface = node.meta.get(_SURFACE)
        if surface:
            pieces.append(f"@surface={surface}")
        return f"{type(node).__name__}({','.join(pieces)})", lits
    if isinstance(node, list):
        parts = [_shape_print(v, read) for v in node]
        return "[" + ",".join(p[0] for p in parts) + "]", [lit for _, found in parts for lit in found]
    return repr(node), []


def sql_shape(sql: str, dialect: str | None = None) -> SqlShape | None:
    """一条查询的形状（见本节开头）：条件的先后、交换律成立处两边的先后、字面量的值都不算；别的不同一律算不同。认不出的为
    None（调用方按 normalize_sql 的文本分组）。从不抛异常。"""
    from sqlglot import exp

    read = _read(dialect)
    text = (_mysql_dashes(sql) if read == "mysql" and isinstance(sql, str) else sql or "").strip()
    root, _ = _parse(text, read)
    if root is None:
        return None
    try:
        if _lost_in_parsing(text, read, root) or _respelled(text, read, root):
            return None
        for i, lit in enumerate(root.find_all(exp.Literal)):
            lit.meta[_LIT_TAG] = i
        order = _normalized_order(root, read)
        if order is None or len(order) != len(normalize_sql(sql, dialect).literals):
            return None
        marked = _mark_surfaces(root, text, read)
        if read == "sqlite":
            _mark_comma_joins(root, text, read)
        if not marked:
            return None
        try:
            tree = _light(root, read, _Schema()).tree
        except _Unsupported:
            # 规范化不了的结构（递归 CTE、不确定的函数……）：只统一名字，条件的先后照原文
            tree = root.copy()
            _normalize_names(tree, read, _Schema())
        printed, lits = _shape_print(tree, read)
    except Exception:  # noqa: BLE001 - 认形状本身的缺陷：当作认不出，调用方按文本分组
        logger.warning("认 SQL 的形状失败", exc_info=True)
        return None
    where = {tag: i for i, tag in enumerate(order)}
    return SqlShape(hashlib.sha256(printed.encode()).hexdigest(),
                    tuple(lit.sql(dialect=read) for lit in lits),
                    tuple(where.get(lit.meta.get(_LIT_TAG)) if lit.meta.get(_LIT_TAG) is not None else None
                          for lit in lits))


__all__ = ["BINDING_LEVELS", "MAX_SQL_CHARS", "PLACEHOLDER", "Aggregate", "ColumnRef", "Computed", "FacetDiff", "Facets",
           "JoinFacet", "LimitFacet", "NormalizedSql", "Part", "Projection", "SqlDiff", "SqlShape", "TemplateBinding",
           "TemplateMatch", "TemplateSlot", "TimePredicate", "bind_template", "catalog_labels", "diff_facets",
           "explain_sql_diff", "facets", "match_template", "normalize_sql", "output_names", "project_select",
           "project_template", "render_bound", "sql_shape"]
