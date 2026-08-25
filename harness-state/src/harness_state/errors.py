"""StateStore 的基础设施错误。"""


class StateStoreError(RuntimeError):
    """StateStore 创建、序列化或数据库操作失败。"""


class StateRecordExistsError(StateStoreError):
    """create() 遇到已经存在的 plan_id。"""


class StateRecordNotFoundError(StateStoreError):
    """save() 的目标 plan_id 尚未创建。"""
