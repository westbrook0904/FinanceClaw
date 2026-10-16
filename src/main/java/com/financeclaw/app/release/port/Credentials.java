package com.financeclaw.app.release.port;

/** Credential material is never part of the immutable release definition. */
public interface Credentials {
  String modelApiKey();

  String resolve(String reference);
}
