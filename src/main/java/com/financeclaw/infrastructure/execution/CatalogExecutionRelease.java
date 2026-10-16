package com.financeclaw.infrastructure.execution;

import com.financeclaw.app.execution.port.ExecutionRelease;
import com.financeclaw.application.ReleaseCatalog;
import com.financeclaw.domain.ExecutionPolicy;
import org.springframework.stereotype.Component;

@Component
public class CatalogExecutionRelease implements ExecutionRelease {
  private final ReleaseCatalog catalog;

  public CatalogExecutionRelease(ReleaseCatalog catalog) {
    this.catalog = catalog;
  }

  public String currentHash() {
    return catalog.currentHash();
  }

  public ExecutionPolicy require(String hash) {
    var r = catalog.require(hash);
    return new ExecutionPolicy(r.scopes(), r.maxModelCalls(), r.maxToolCalls());
  }
}
