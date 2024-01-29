# ZhiArchive

**监测知乎用户的个人动态并保存内容以防丢失。**


某用户的动态结果保存目录如下：
`activities`为个人动态页快照，`archives`为动态对应的回答/文章快照
```
.
├── activities
│   ├── 2024
│   │   └── 01
│   │       └── 17
│   │           ├── 回答-为什么只有饿死的狮子而没有饿死的老虎？说明了什么问题？.png
│   │           ...
│   │           └── 赞同-如何看待211高校华中某业大学动物Y养系黄某若教授十几年如一日的学术造假行为？.png
│   └── 20240117181850.json
└── archives
    └── 2024
        └── 01
            └── 17
                ├── 回答-为什么只有饿死的狮子而没有饿死的老虎？说明了什么问题？
                │   ├── info.json
                │   └── 回答-为什么只有饿死的狮子而没有饿死的老虎？说明了什么问题？.png
                ...
                └── 赞同-如何看待211高校华中某业大学动物Y养系黄某若教授十几年如一日的学术造假行为？
                    ├── info.json
                    └── 赞同-如何看待211高校华中某业大学动物Y养系黄某若教授十几年如一日的学术造假行为？.png

16 directories, 25 files
```
其中：
**动态**文件`activities/2024/01/17/赞同-如何看待211高校华中某业大学动物Y养系黄某若教授十几年如一日的学术造假行为？.png`如图：
![Pasted image 20240117182702](https://github.com/amchii/attachments/assets/26922464/af79089c-ec57-4305-964d-1dbbc99716c7)



**目标**文件`archives/2024/01/17/赞同-如何看待211高校华中某业大学动物Y养系黄某若教授十几年如一日的学术造假行为？/赞同-如何看待211高校华中某业大学动物Y养系黄某若教授十几年如一日的学术造假行为？.png`如图：
![Pasted image 20240117182908](https://github.com/amchii/attachments/assets/26922464/7a62a61c-8323-419b-976e-fcb396aaaa13)


`archives/2024/01/17/赞同-如何看待211高校华中某业大学动物Y养系黄某若教授十几年如一日的学术造假行为？/info.json`内容为：

```
{
  "title": "如何看待211高校华中某业大学动物Y养系黄某若教授十几年如一日的学术造假行为？",
  "url": "https://zhuanlan.zhihu.com/p/678136207",
  "author": "zhang-li-28-1",
  "shot_at": "2024-01-17T18:19:13.783"
}
```
## 它是如何工作的

`ZhiArchive`使用[Playwright](https://github.com/microsoft/playwright)，它由4个部分组成，分别是monitor，archiver，login worker和api：

- **monitor**：用于监测用户个人主页的动态并将新的动态：打快照，把动态的目标（回答、文章）链接通过redis丢给**archiver**。
- **archiver**：打开目标链接并保存屏幕快照至本地。
- **login worker**：用于登录知乎获取**monitor**和**archiver**所必需的认证信息。
- **api**：提供接口来操作控制**monitor**，**archiver**，**login worker**。
## 使用

*注意查看日志跟踪运行状态*
*archiver: archiver.log*
*monitor: monitor.log*
*login_worker: login_worker.log*

### Docker
#### 下载本项目：
```sh
# 下载本项目
git clone https://github.com/amchii/ZhiArchive.git
# 进入项目目录
cd ZhiArhive
```
#### 构建镜像:
```sh
docker build -t zhi-archive:latest -f BaseDockerfile .
```
#### 配置环境变量：
  所有可配置项见[config.py](https://github.com/amchii/ZhiArchive/blob/dev/archive/config.py)，支持通过环境变量或`.env`，`.apienv`文件配置

`.env`文件
```
secret_key=  # 请生成一个随机字符串
people=<someone>  # 知乎用户，在个人主页地址中：https://www.zhihu.com/people/<someone>
monitor_fetch_until=10  # 天数，Monitor初次运行时默认抓取到10天前的动态
```
`.apienv`文件
```
# API认证账号，配置用户名和密码
username=
password=
```

#### 启动
```
docker compose up -d
```
API端口为9090，以127.0.0.1为例，
打开[http://127.0.0.1:9090/docs](http://127.0.0.1:9090/docs)可查看接口文档，下面👇🏻所提到的接口可在这个接口文档进行调用，调用之前请先打开[http://127.0.0.1:9090/auth/login](http://127.0.0.1:9090/auth/login)登录获取本项目的接口认证信息（Cookies）

#### 登录知乎获取Cookie
打开[http://127.0.0.1:9090/zhi/login](http://127.0.0.1:9090/zhi/login)获取知乎登录二维码：
![image](https://github.com/amchii/attachments/assets/26922464/11e0b5a6-b17f-44ae-8cc1-b89631a1358e)

扫码完成登录后将重定向到"http://127.0.0.1:9090/zhi/login/state/f19c99849de8dccc8e9b" 并显示获取的cookies，路径最后的'f19c99849de8dccc8e9b'将是你的state文件地址，文件存储路径为`<项目目录>/states/f19c99849de8dccc8e9b.state.json`，可通过接口`GET/PUT /zhi/core/state_path` 查看和设置正在运行的`Monitor`和`Archiver`的state文件。
*（后续考虑登录完成即设置state）*

#### 运行Monitor和Archiver
Monitor和Archiver默认是暂停状态，设置好知乎的Cookie后，可以通过接口：
`/zhi/core/{name}/pause`查看和更改运行状态，`name`可以是'monitor'或'archiver'
运行后查看日志输出和结果目录。


## TODO

- 所有元素selector可配置
- 通过接口完全控制`Monitor`, `Archiver`
- 支持监测多个用户
- 异常告警
- 提供前端界面


## 欢迎交流，Star⭐️一下，随时更新
