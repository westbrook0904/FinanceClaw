"""安全领域错误，不携带出生资料或第三方异常正文。"""


class ZiweiError(ValueError):
    """供紫微图生成澄清或不支持结果的可公开领域异常。

    code 是稳定分类，message 是可展示说明，fields 是需要补充的输入路径
    （如 birth.time）。调用方不得把原始出生资料或第三方异常正文拼入这些字段；
    graph 使用它们构造 ZiweiAgentResult，而不是向父 Agent 泄漏内部异常。
    """

    def __init__(self, code: str, message: str, fields: tuple[str, ...] = ()) -> None:
        """错误本身不拼接原始输入，以免进入异常日志。"""
        super().__init__(message)
        self.code = code
        self.fields = fields
