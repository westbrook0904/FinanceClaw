package com.financeclaw.infrastructure.ziwei;

import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.app.ziwei.port.ZiweiConfiguration;
import com.financeclaw.domain.release.ZiweiPolicy;
import org.springframework.stereotype.Component;

@Component
public class CatalogZiweiConfiguration implements ZiweiConfiguration {
  private final ReleaseService catalog;
  private final com.financeclaw.app.release.port.Credentials credentials;

  public CatalogZiweiConfiguration(
      ReleaseService catalog, com.financeclaw.app.release.port.Credentials credentials) {
    this.catalog = catalog;
    this.credentials = credentials;
  }

  public ZiweiPolicy policy(String releaseHash) {
    return catalog.require(releaseHash).business().ziwei();
  }

  public byte[] key(String reference) {
    return ZiweiPolicy.key(credentials.resolve(reference));
  }
}
