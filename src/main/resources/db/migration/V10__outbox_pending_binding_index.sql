-- 定时领取先锁定 binding，再按该绑定取得最早可锁通知。
-- 保留已有 V1–V9 校验值；索引也必须随已初始化的数据库前向升级。
CREATE INDEX channel_pending_notice_binding
    ON channel.notices(binding_id,created_at) WHERE status='pending';
