package com.financeclaw.infrastructure.release;

import java.nio.charset.StandardCharsets;
import java.nio.file.*;

public final class BusinessResources {
  private BusinessResources() {}

  public static String read(String location) {
    try {
      if (location.startsWith("classpath:")) {
        try (var in =
            BusinessResources.class.getClassLoader().getResourceAsStream(location.substring(10))) {
          if (in == null) throw new IllegalArgumentException("Missing packaged resource");
          return new String(in.readNBytes(2097153), StandardCharsets.UTF_8);
        }
      }
      if (Files.size(Path.of(location)) > 2097152)
        throw new IllegalArgumentException("Resource too large");
      return Files.readString(Path.of(location));
    } catch (java.io.IOException e) {
      throw new IllegalArgumentException("Cannot load business resource");
    }
  }
}
