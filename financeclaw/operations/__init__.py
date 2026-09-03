"""运维命令包：仅存放 smoke 冒烟、在线探针与评测数据命令等运维脚本。

本包不承载业务用例（业务逻辑位于 application 层），各脚本通过
``python -m financeclaw.operations.<模块>`` 运行：跨重启会话冒烟
``conversation_smoke``、记忆冒烟 ``memory_smoke``、工作流冒烟
``workflow_smoke``、Provider 在线探针 ``provider_probe``、本地 Agent Server
冒烟 ``server_smoke``，以及评测种子命令 ``memory_eval_seed`` 与
``workflow_eval_seed``。
"""
