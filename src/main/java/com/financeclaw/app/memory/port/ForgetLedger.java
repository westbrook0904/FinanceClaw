package com.financeclaw.app.memory.port;

import com.financeclaw.domain.memory.ForgetFact;
import java.util.List;

/**
 * Independently committed, append-only privacy authority. Never shares the business transaction.
 */
public interface ForgetLedger extends AutoCloseable {
  boolean enabled();

  void initialize(String lineage, List<ForgetFact> history);

  void append(ForgetFact fact);

  List<ForgetFact> snapshot(String lineage);

  default void close() {}
}
