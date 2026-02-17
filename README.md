# 🥚 创作之蛋

一个专为创作型 Discord 社区设计的核心助理机器人。它基于最新的 `discord.py` 框架构建，深度整合了对论坛 (Forum) 功能的支持，旨在提供强大的内容保护、便捷的作品探索和趣味性的社区互动体验。

## ✨ 主要功能

-   **附件保护与溯源系统 (`protection`)**:
    -   **自定义解锁条件**: 贴主可以为附件设置多种获取条件，如 **点赞**、**评论** 或 **口令** 的自由组合。
    -   **数字水印与溯源**: 所有通过保护系统分发的文件都会被自动注入独一无二的“幽灵追踪”指纹。管理员可以通过上传文件，快速溯源其原始下载者，有效遏制恶意传播。
    -   **发布与管理面板**: 提供完善的 UI 界面，方便贴主上传附件、修改标题、重命名文件、调整解锁条件，或删除已发布的附件。

-   **社区内容探索中心 (`exploration`)**:
    -   **多功能搜索引擎**: 提供强大的帖子搜索功能，支持按 **关键词**、**作者**、**指定分区** 乃至 **分区标签** 进行组合搜索，并以可翻页的列表展示结果。
    -   **每日更新日报**: 自动监控全服务器当天发布的新帖子，并生成日报面板，方便成员快速了解社区最新动态。

-   **缘分推荐与抽卡 (`recommend`)**:
    -   **每日精选推荐**: 每天从社区中挑选一个优质帖子进行展示，增加优秀作品的曝光度。
    -   **用户抽卡系统**: 成员可以通过“抽卡”来随机获取一个帖子推荐。支持**单抽**、**五连抽**和**十连抽**，为发现内容增添了趣味性。

## 🚀 快速开始 (开发环境搭建)

按照以下步骤，你可以在本地快速启动并开始开发“创作之蛋”。

### 1. 克隆仓库

```bash
git clone [你的仓库链接]
cd CREATE
```

### 2. 创建并激活虚拟环境

使用虚拟环境是管理项目依赖的最佳实践。

-   **Windows**:
    ```bash
    python -m venv venv
    .\venv\Scripts\activate
    ```
-   **macOS / Linux**:
    ```bash
    python3 -m venv venv
    source venv/bin/activate
    ```

### 3. 安装依赖

所有必需的 Python 库都记录在 `requirements.txt` 中。

```bash
pip install -r requirements.txt
```

### 4. 配置机器人

机器人需要一些关键信息才能运行。

-   **步骤一：创建 `.env` 文件**:
    在项目根目录 (`CREATE/`) 创建一个名为 `.env` 的文件，并填入你的机器人 Token。
    ```env
    DISCORD_TOKEN="你的机器人TOKEN粘贴到这里"
    ```

-   **步骤二：配置 `config.py`**:
    打开 `config.py` 文件，根据你的服务器情况，修改里面的 `TEST_GUILDS` 列表，填入你的**测试服务器ID**。这能让你在开发时，斜杠命令**立即同步**，无需漫长等待。

    ```python
    # config.py
    TEST_GUILDS = [
        123456789012345678, # <- 替换成你的测试服务器ID
        # 如果有多个测试服，可以继续添加
    ]
    ```
    同时，你可以在此文件中配置其他全局常量。

### 5. 运行机器人

一切就绪！现在可以启动机器人了。

```bash
python main.py
```

### 6. 同步斜杠命令 (重要！)

我们采用了更安全、更专业的**手动同步**策略。当你首次启动，或新增/修改了命令后，请在你的测试服务器的任意频道中，发送以下指令：

```
!forcesync
```

看到机器人回复“同步完成”后，**重启你的 Discord 客户端 (Ctrl + R)**，就能看到最新的斜杠命令了。

## 🏗️ 项目结构

“创作之蛋”采用高度模块化的 `Cogs` 架构。每个核心功能都被封装在一个独立的文件夹中，使得代码逻辑清晰，易于维护和扩展。

```
CREATE/
├── core/                      # 核心与共享模块
│   ├── db.py                  # 全局数据库连接与初始化
│   └── utils.py               # 通用工具函数
│
├── cogs/                      # 机器人所有功能模块（魔法书）的存放处
│   ├── protection/            # ⭐ 附件保护与溯源模块 (核心功能)
│   │   ├── cog.py             # 命令、事件监听器
│   │   ├── utils.py           # 专用于此模块的工具函数 (如注入指纹)
│   │   └── ui/                # UI组件 (Views 和 Modals)
│   │       ├── modals.py
│   │       └── views.py
│   │
│   ├── exploration/           # 🧩 作品探索模块
│   └── recommend/             # ✨ 推荐与抽卡模块
│── core/                      # 通用共享模块
│   ├── db.py
│   ├── utils.py
├── .env                       # (需自行创建) 存放机器人TOKEN等敏感信息
├── config.py                  # 全局配置文件，存放固定的ID和常量
├── main.py                    # 机器人主入口，负责加载Cogs和启动
└── requirements.txt           # 项目依赖库列表
```

**通用模块设计模式**:
-   `cog.py`: 模块的核心业务逻辑，包含所有斜杠命令 (`@app_commands.command`) 和事件监听器 (`@commands.Cog.listener`)。
-   `setup` 函数 (`cog.py`内): 每个 `cog.py` 文件都包含一个 `async def setup(bot)` 函数，这是模块被 `main.py` 加载的入口。
-   `ui/views.py` & `ui/modals.py`: 分别存放与该模块相关的 UI 组件，如 `discord.ui.View`, `discord.ui.Modal` 等，实现逻辑与视图分离。
-   `db.py` / `utils.py`: 专属于该模块的数据操作和辅助函数，增强了模块的独立性。

## 🛠️ 如何新增功能

得益于模块化设计和自动加载机制，添加新功能非常简单：

1.  在 `cogs/` 目录下创建一个新文件夹，例如 `cogs/new_feature`。
2.  在该文件夹内，创建 `cog.py` 文件。
3.  在 `cog.py` 中编写你的新功能代码，并确保包含一个 `Cog` 类和一个 `setup` 函数。
    ```python
    # cogs/new_feature/cog.py
    from discord.ext import commands

    class NewFeatureCog(commands.Cog):
        def __init__(self, bot):
            self.bot = bot

        @commands.command()
        async def hello(self, ctx):
            await ctx.send("Hello from a new feature!")

    async def setup(bot):
        await bot.add_cog(NewFeatureCog(bot))
    ```
4.  重新启动机器人，`main.py` 会自动扫描并加载你的新模块。
5.  在测试服务器使用 `!forcesync` 命令，然后重启客户端查看新命令。