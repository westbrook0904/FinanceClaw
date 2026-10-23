package com.financeclaw.app.memory.port;

import com.financeclaw.domain.memory.ForgetFact;
import java.util.List;

/** 独立追加式遗忘账本：每次写入自行提交，绝不与业务数据库共享事务或依赖业务备份恢复。 */
public interface ForgetLedger extends AutoCloseable {
    boolean enabled();

    void initialize(String lineage, List<ForgetFact> history);

    void append(ForgetFact fact);

    List<ForgetFact> snapshot(String lineage);

    /** 关闭独立账本持有的资源；禁用账本的实现不持有连接池。 */
    @Override
    default void close() {}
}
