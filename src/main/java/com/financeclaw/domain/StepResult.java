package com.financeclaw.domain;

/** Persisted result of an idempotent local capability step. */
public record StepResult(String inputHash, String result) {}
