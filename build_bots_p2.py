# -*- coding: utf-8 -*-
html_body = """
<body>
<header>
  <div class=\"logo\">&#x1F916;</div>
  <h1>机器人配置管理</h1>
  <div class=\"nav-links\">
    <a href=\"/admin/\" class=\"nav-link\">&#x270F; Persona</a>
    <a href=\"/admin/bots\" class=\"nav-link active\">&#x1F916; 机器人配置</a>
    <a href=\"/admin/ingest\" class=\"nav-link\">&#x1F4E6; 数据入库</a>
  </div>
</header>
<div class=\"layout\">
  <aside class=\"sidebar\">
    <div class=\"sidebar-header\">
      <span>机器人列表</span>
      <button class=\"btn-new\" onclick=\"newBot()\">+ 新建</button>
    </div>
    <div class=\"bot-list\" id=\"botList\"><div class=\"empty-list\">暂无配置</div></div>
  </aside>
  <div class=\"main\">
    <div id=\"emptyState\" class=\"empty-state\">
      <div class=\"icon\">&#x1F916;</div>
      <p>选择或新建一个机器人配置开始编辑</p>
    </div>
    <div id=\"editorWrapper\" style=\"display:none;flex-direction:column;height:100%\">
      <div class=\"toolbar\">
        <span class=\"title\" id=\"editorTitle\">新建机器人</span>
        <button class=\"btn\" onclick=\"previewPrompt()\">&#x1F441; 预览 Prompt</button>
        <button class=\"btn\" onclick=\"copyApiSnippet()\">&#x1F4CB; API 示例</button>
        <button class=\"btn primary\" onclick=\"saveBot()\">&#x1F4BE; 保存</button>
        <button class=\"btn danger\" id=\"btnDelete\" style=\"display:none\" onclick=\"deleteBot()\">&#x1F5D1; 删除</button>
      </div>
      <div class=\"editor-area\">
        <div class=\"form-panel\">
          <div class=\"id-row\">
            <span class=\"id-label\">Bot ID</span>
            <input type=\"text\" id=\"botId\" placeholder=\"cs-bot / sales-bot\"/>
          </div>
          <div class=\"section-title\">&#x1F4DA; 知识库挂载</div>
          <div class=\"col-box\">
            <div class=\"col-title\">&#x1F7E2; Collection 名称</div>
            <div class=\"field\">
              <input type=\"text\" id=\"f_collection\" placeholder=\"agent_memory\"/>
              <span class=\"hint\">对应 Milvus Collection 名称，不同机器人可挂载不同知识库</span>
            </div>
            <div class=\"col-presets\">
              <span style=\"font-size:.7rem;color:var(--muted)\">快速选择：</span>
              <span class=\"col-preset\" onclick=\"setCollection('agent_memory')\">agent_memory</span>
              <span class=\"col-preset\" onclick=\"setCollection('cs_knowledge')\">cs_knowledge</span>
              <span class=\"col-preset\" onclick=\"setCollection('sales_products')\">sales_products</span>
              <span class=\"col-preset\" onclick=\"setCollection('faq_base')\">faq_base</span>
            </div>
          </div>
          <div class=\"section-title\">&#x1F3AD; 角色身份</div>
          <div class=\"form-grid\">
            <div class=\"field\"><label>客服名字</label><input id=\"f_name\" placeholder=\"小美\"/></div>
            <div class=\"field\"><label>角色定位</label><input id=\"f_role\" placeholder=\"电商客服助手\"/></div>
            <div class=\"field\" style=\"grid-column:1/-1\"><label>品牌名称</label><input id=\"f_brand\" placeholder=\"优品电商\"/></div>
          </div>
          <div class=\"section-title\">品牌介绍</div>
          <div class=\"field\"><textarea id=\"f_brand_intro\" rows=\"3\" placeholder=\"品牌定位、主营类目...\"></textarea></div>
          <div class=\"section-title\">性格特征 <span class=\"hint\">（Enter 添加）</span></div>
          <div class=\"tag-wrap\" onclick=\"document.getElementById('ti_traits').focus()\">
            <div id=\"tags_traits\"></div><input class=\"tag-input\" id=\"ti_traits\" placeholder=\"亲切温柔\" onkeydown=\"addTag(event,'traits')\"/>
          </div>
          <div class=\"section-title\">话术规范 <span class=\"hint\">（Enter 添加）</span></div>
          <div class=\"tag-wrap\" onclick=\"document.getElementById('ti_style').focus()\">
            <div id=\"tags_style\"></div><input class=\"tag-input\" id=\"ti_style\" placeholder=\"使用「亲」「您」称谓\" onkeydown=\"addTag(event,'style')\"/>
          </div>
          <div class=\"section-title\">闲聊引导话题 <span class=\"hint\">（Enter 添加）</span></div>
          <div class=\"tag-wrap\" onclick=\"document.getElementById('ti_topics').focus()\">
            <div id=\"tags_topics\"></div><input class=\"tag-input\" id=\"ti_topics\" placeholder=\"产品推荐\" onkeydown=\"addTag(event,'topics')\"/>
          </div>
          <div class=\"section-title\">额外业务指令</div>
          <div class=\"field\">
            <textarea id=\"f_extra\" rows=\"4\" placeholder=\"主推玫瑰系列，价格策略不低于官网价...\"></textarea>
            <span class=\"hint\">直接注入 System Prompt，可传达特殊业务规则</span>
          </div>
        </div>
        <div class=\"preview-panel\">
          <div class=\"preview-header\"><span>Prompt 预览</span><button class=\"btn\" onclick=\"copyPreview()\">&#x1F4CB; 复制</button></div>
          <div class=\"preview-body\" id=\"previewBody\"><span style=\"color:var(--muted);font-family:var(--sans);font-style:italic\">点击「预览 Prompt」查看渲染结果...</span></div>
        </div>
      </div>
    </div>
  </div>
</div>
<div id=\"toast\"></div>
"""
open('api/static/bots_p2.html','w',encoding='utf-8').write(html_body)
print('p2:', len(html_body))
