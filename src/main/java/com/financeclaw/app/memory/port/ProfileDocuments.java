package com.financeclaw.app.memory.port;

import com.financeclaw.domain.Task.Owner;

/**
 * 按用户和隐私 epoch 隔离的人工 Markdown 文件入口。
 *
 * <p>文件在新 Turn 受理或人工查看时同步版本，仅受理时创建 Turn 快照；执行恢复不重新选择文件， 遗忘只能清理指定 epoch 及更旧的托管文档。
 */
public interface ProfileDocuments {
    boolean enabled();

    String read(Owner owner, long epoch);

    void eraseThrough(Owner owner, long epoch);

    /** 未配置服务器目录时保留原数据库/API维护方式，不进行文件访问。 */
    static ProfileDocuments disabled() {
        return new ProfileDocuments() {
            @Override
            public boolean enabled() {
                return false;
            }

            @Override
            public String read(Owner owner, long epoch) {
                return "";
            }

            @Override
            public void eraseThrough(Owner owner, long epoch) {}
        };
    }
}
