package com.financeclaw.app.task.port;

import com.financeclaw.domain.Task.Turn;

/** 每次交付前重验订阅身份；实现不得跨帧缓存授权或隐私版本。 */
@FunctionalInterface
public interface TaskEventAccess {
    void check(Turn subscribed);
}
