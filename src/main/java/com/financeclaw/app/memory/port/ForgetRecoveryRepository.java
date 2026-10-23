package com.financeclaw.app.memory.port;

import com.financeclaw.domain.Memory.Privacy;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.memory.ForgetFact;
import java.util.List;

/** 主库遗忘屏障和恢复回执的边界：保存待追加事实并按 epoch 擦除，恢复完成前核验残留与历史一致性。 */
public interface ForgetRecoveryRepository {
    record Binding(String lineage, boolean active) {}

    Binding binding();

    Binding lockBinding();

    void activate();

    ForgetFact pending(Owner owner);

    List<ForgetFact> pending();

    void prepare(ForgetFact fact);

    /** 调用方持有用户锁后，按遗忘 epoch 执行幂等擦除并保存稳定回执。 */
    void apply(ForgetFact fact);

    /** 读取本地主库的权威遗忘审计及幂等回执，用于首次初始化独立账本时核对历史。 */
    List<ForgetFact> history();

    List<Privacy> owners();

    /** 只在重放及残留校验完成后解除屏障；仍有准备中请求的用户必须保持阻断。 */
    void complete(Owner owner);

    /** 统计无效 epoch、孤立用户、过期正文或索引、未清理召回或任务及准备中请求。 */
    long unsafeRows();
}
