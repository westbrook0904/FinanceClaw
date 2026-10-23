package com.financeclaw.infrastructure.ziwei;

import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.app.ziwei.port.ZiweiConfiguration;
import com.financeclaw.domain.release.ZiweiPolicy;
import org.springframework.stereotype.Component;

/** 实现 ZiweiConfiguration，从指定 releaseHash 读取固定紫微策略。 密钥通过凭据端口按引用解析并校验长度，不从工具输入接受密钥或临时修改发布规则。 */
@Component
public class CatalogZiweiConfiguration implements ZiweiConfiguration {
    private final ReleaseService catalog;
    private final com.financeclaw.app.release.port.Credentials credentials;

    public CatalogZiweiConfiguration(
            ReleaseService catalog, com.financeclaw.app.release.port.Credentials credentials) {
        this.catalog = catalog;
        this.credentials = credentials;
    }

    @Override
    public ZiweiPolicy policy(String releaseHash) {
        return catalog.require(releaseHash).business().ziwei();
    }

    @Override
    public byte[] key(String reference) {
        return ZiweiPolicy.key(credentials.resolve(reference));
    }
}
