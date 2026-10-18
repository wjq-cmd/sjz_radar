"""Write the release validation report and SHA256 manifest from local evidence."""
from pathlib import Path
import hashlib,json
ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'验证'
web=json.loads((OUT/'webgl-results.json').read_text(encoding='utf-8'))
published=json.loads((OUT/'published-final/result.json').read_text(encoding='utf-8'))
assert web['passed'] and not web['errors'] and published['packaged3dModulesMatchSource']
assert '"checks":410' in (OUT/'selftest.log').read_text(encoding='utf-8-sig')
for name in ('parser-tests.log','timeline-tests.log'):
    assert '\nOK' in (OUT/name).read_text(encoding='utf-8-sig')
report='''# V5.1.1 修复与验证记录

2026-09-19。交付目录包含源码、程序、资料、验证；V5.1.0 和旧采集记录独立保留。

## 定位到的问题

1. 3D 按 is_bot 二次分组，把 ER 的玩家型人机当作 AI；关闭 AI 会漏掉 2D 仍保留的人物。现在按 ER kind 分类，玩家型人机保留在人物池，死亡盒也不再误归入 AI。
2. 人物池按数组序号复用，旧版位置逐帧向目标靠近；当人物顺序变化时，会从前一个人的位置滑向后一个人。现在渲染位置直接使用当前呈现快照，身份按实体标识维护。重复快照不重启插值，回放真人使用单一连续时间线，最长 80ms；换局、跳转、重用对象和大位移直接对齐。
3. 第一视角原先保留鼠标方向，未持续读取自身瞄准 yaw/pitch。现在每帧跟随当前方向，俯仰使用 UE 正角度向上的约定；新地图加载也保留跟随镜头。
4. 圆形黑洞来自 Korr 室内裁切着色器。现在地面和竖直面分组，竖直面不透明度 0.22、不写深度，取消裁切及冲突的屏幕深度效果。保留完整地图网格。
5. 3D 原先没有 2D 的“超距·最后位置”标记。现在也显示并减淡该标签；它代表最后一次已知位置。

## 已通过的验证

- 实际 WebGL：用户保留状态中两名玩家型人机的身份、XYZ 与 2D 源快照逐项对应。
- 实际 WebGL：130 名同名测试人物、真假 AI 分类、队友开关、人物顺序反转和大距离变动，身份及渲染 XYZ 正确；超距标签存在。
- 实际 WebGL：5 组连续变化的 yaw/pitch，第一视角实际相机方向与理论方向一致，眼睛位置包含设定高度。
- 实际 WebGL：无索引几何与真实大坝地图均保留三角面；真实地图 5,293,593 面，其中水平面 1,934,223、竖直面 3,359,370；无圆形裁切。浏览器错误日志为空。
- 正式网页：离线回放末段显示 3D人物 11 / 应显示 11 / 其中真人 2；上行 1558、下行 2296，输入异常及序号缺口为 0。
- 发布包：HTTP 提供的三个 3D 模块与最终源码逐字节相同。明文 3854 包、人物及物资共 30 个实体、自身姿态 927 条；分块管道输入、实时状态前进、异常输入、端口占用、正常退出及父进程退出均通过。
- 主程序 410 项自测通过；解析器 10 项回归及时间轴/归档/容器 3 项回归通过。

日志：webgl-results.json、geometry-tests.log、fix-tests.log、parser-tests.log、timeline-tests.log、published-final/result.json、selftest.log、build.log。

## 验证范围

本轮使用已有离线样本、保留状态与合成人物序列，没有重新连接手机或改变正在运行的旧会话。渲染 XYZ 对齐的是解析快照；每张地图与真实角色脚底/层高的精确贴合仍需相应场景核对。网络未发送的新坐标不会由渲染插值生成，超距对象继续保留最后已知位置。

运行：打开 程序/Nva本地离线解析.exe，选择设备后直接进入实时解析。网页沿用 17891。程序、NvaAssets.pak、models 和 WinDivert 文件必须整体保留。
'''
(OUT/'修复验证报告.md').write_text(report,encoding='utf-8')
manifest=[]
for directory in ('程序','源码'):
    for path in sorted((ROOT/directory).rglob('*')):
        relative=path.relative_to(ROOT)
        if not path.is_file() or any(p in ('bin','obj','build','__pycache__') for p in relative.parts):continue
        manifest.append({'file':relative.as_posix(),'bytes':path.stat().st_size,'sha256':hashlib.file_digest(path.open('rb'),'sha256').hexdigest()})
(OUT/'release-sha256.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'files':len(manifest),'releaseMiB':round(sum(p.stat().st_size for p in (ROOT/'程序').rglob('*') if p.is_file())/1048576,1),'webglPassed':True,'packagePassed':True},ensure_ascii=False))
