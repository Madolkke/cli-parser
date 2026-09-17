"""Experimental lightweight Schema draft guidance; direct guidance is unchanged."""

from .prompt import SCHEMA_SYSTEM_PROMPT


def _draft_guidance() -> str:
    prompt = (
        SCHEMA_SYSTEM_PROMPT.replace(
            "只通过 submit_result_schema 提交产物。普通 assistant 文本不会被视为产物。",
            "只通过 submit_schema_draft 提交完整 draft。"
            "普通 assistant 文本不会被视为产物。",
        )
        .replace(
            "- 使用 JSON Schema Draft 2020-12，根类型必须是 object。"
            "它描述单份命令输出的\n"
            "  一个解析后 record，而不是服务响应或样例列表。",
            "- 程序将草稿编译为 Draft 2020-12 Schema，固定根 object。每份完整命令输出\n"
            "  对应一个根 record；输入样例数量不等于业务实体数量。"
            "根共享标题及汇总留在根，\n"
            "  输出内部的重复行或详情块放入根内或所属实体内的 array。"
            "第二个实体及其子项\n"
            "  必须有明确归属。平面设备属性可直接用根字段，"
            "不强行增加数组或样例编号容器。",
        )
        .replace(
            "- 每个 object 都要将 additionalProperties 设置为 false。"
            "字段名必须是英文 ASCII",
            "- 程序将每个 object 的 additionalProperties 固定为 false。"
            "字段名必须是英文 ASCII",
        )
    )
    prompt = prompt.replace(
        "工具参数只有 result_schema；required/properties/type "
        "必须位于其内部正确节点。"
        "不要添加 arguments 字符串包装，也不要把 Schema 关键字放在工具参数顶层。",
        "以上能力描述的是编译后的 Schema。工具参数只有 draft；草稿结构与 attributes "
        "按下述规则填写，不要添加 arguments 字符串包装。",
    )
    return (
        prompt
        + """
本阶段提交的是轻量草稿，不是任意 JSON Schema；上述类型、required、业务值粒度、
覆盖和说明政策继续适用于最终契约。不要计算字符坐标；无需在草稿中提交逐实例清单或
值槽证据，required 仍按上述可见实例逐一判断。

草稿工具唯一顶层键为 draft。draft 为 {"version":1,"fields":[...]}，可选 attributes。
每个字段为 {"name":名称来源,"required":布尔值,"node":节点}，required 由你根据可见
实例判断。节点 type 只允许 object、array、string、integer、number、boolean。
object 节点使用 fields 列表，array 节点使用单一 items 节点；标量不加 fields/items。
匿名 items 节点只有 type 及适用节点属性，没有 name 或 required。草稿最多 64 KiB UTF-8、
256 个字段、16 层编译后嵌套、1024 个标签引用；超限会拒绝，不得静默删字段。
标题、description、enum 和其他该类型原本允许的校验约束放进 attributes；不要把
$schema、type、properties、required、additionalProperties、items 放入 attributes。
程序只负责结构与标签规范化，不代替你判断语义、类型、required 或说明的合理性。

名称来源：
- 有明确英文标签时，name={"source":[{"line_id":"实际行号","quote":"原文标签"}]}。
  quote 必须来自实际展示的同一行且定位唯一；只引用标签或明确限定，不能引用业务值。
  只用 complete=true 行，不截断英文单词；同一名称的各引用必须在同一输入、同一
  采样片段中按原文顺序排列且互不重叠，不能跨缺口拼接标签。
  程序按来源顺序保留标签词序、缩写及业务限定，只统一 ASCII 小写 snake_case。
- 多行表头先依据列对齐确定限定归属，再按上层到下层顺序引用；邻列或整表标题不能
  机械加入。不能因父容器已表达相同对象而删掉标签限定，也不添加原文没有的前缀。
- 同一父对象重名时先补最近且明确的原文限定；不得覆盖、截断、随意编号或改变层级。
- 无可靠标签或确需语义名称时用 name={"fallback":"合法名称","reason":"原因"}。
  原因仅限 unlabeled、ambiguous_source、invalid_name、conflict、split_component；
  invalid_name 和 conflict 必须附 source，且原标签规范结果确实非法或在同父碰撞；
  其他原因的业务必要性仍需复核。没有标签的实体数组可用 unlabeled；拆分子项用
  split_component；合法性或冲突才使用相应例外。不能为偏好同义词而绕开真实标签。
- 名称兜底仍须满足既有字符、长度、Python 关键字及标量 ignore 门禁；不自动单复数。

以下为独立合成正例，只说明完整根、重复实体、可选重复子项及轻量字段声明。实际来源
行号以任务中展示为准。Batch/Total 属于根，Checks 属于各 Asset，Tail 仍在父实体。
Result 的符号与明确空槽都保留，缺失行不虚构值；名称来源无需重复引用每个实例。
<draft_example_input>
Batch: winter
Asset: Cedar
Result: ready
Checks:
Check: voltage
Outcome: !pending
Check: status
Outcome:
Tail: one
Asset: Birch
Asset: Elm
Result: !hold
Checks:
Check: route
Outcome: pass
Tail: three
Asset: Aspen
Result:
Tail: four
Total: 4
</draft_example_input>
<draft_example_arguments_json>
{"draft":{"version":1,"fields":[
{"name":{"source":[{"line_id":"i0f0l1","quote":"Batch"}]},"required":true,"node":{"type":"string"}},
{"name":{"fallback":"assets","reason":"unlabeled"},"required":true,"node":{"type":"array","items":{"type":"object","fields":[
{"name":{"source":[{"line_id":"i0f0l2","quote":"Asset"}]},"required":true,"node":{"type":"string"}},
{"name":{"source":[{"line_id":"i0f0l3","quote":"Result"}]},"required":false,"node":{"type":"string","attributes":{"description":"保留结果字符串和值内符号；明确空槽保留空字符串，缺失行省略字段。"}}},
{"name":{"source":[{"line_id":"i0f0l4","quote":"Checks"}]},"required":false,"node":{"type":"array","items":{"type":"object","fields":[
{"name":{"source":[{"line_id":"i0f0l5","quote":"Check"}]},"required":true,"node":{"type":"string"}},
{"name":{"source":[{"line_id":"i0f0l6","quote":"Outcome"}]},"required":true,"node":{"type":"string"}}
]}}},
{"name":{"source":[{"line_id":"i0f0l9","quote":"Tail"}]},"required":false,"node":{"type":"string"}}
]}}},
{"name":{"source":[{"line_id":"i0f0l20","quote":"Total"}]},"required":true,"node":{"type":"integer"}}
]}}
</draft_example_arguments_json>
"""
    )


SCHEMA_DRAFT_SYSTEM_PROMPT = _draft_guidance()
