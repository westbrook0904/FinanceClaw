package com.financeclaw.architecture;

import com.tngtech.archunit.core.domain.Dependency;
import com.tngtech.archunit.core.domain.JavaClasses;
import java.util.*;

/**
 * Records concrete class edges participating in package cycles, including indirect dependencies.
 */
public final class PackageCycles {
  private PackageCycles() {}

  public static Set<String> edges(JavaClasses classes) {
    var dependencies =
        classes.stream()
            .flatMap(c -> c.getDirectDependenciesFromSelf().stream())
            .filter(d -> d.getOriginClass().getPackageName().startsWith("com.financeclaw."))
            .filter(d -> d.getTargetClass().getPackageName().startsWith("com.financeclaw."))
            .filter(
                d ->
                    !d.getOriginClass()
                        .getPackageName()
                        .equals(d.getTargetClass().getPackageName()))
            .toList();
    Map<String, Set<String>> graph = new HashMap<>();
    for (var d : dependencies)
      graph
          .computeIfAbsent(d.getOriginClass().getPackageName(), ignored -> new HashSet<>())
          .add(d.getTargetClass().getPackageName());
    var result = new TreeSet<String>();
    for (Dependency d : dependencies) {
      if (reaches(
          graph,
          d.getTargetClass().getPackageName(),
          d.getOriginClass().getPackageName(),
          new HashSet<>()))
        result.add(d.getOriginClass().getName() + " -> " + d.getTargetClass().getName());
    }
    return result;
  }

  private static boolean reaches(
      Map<String, Set<String>> graph, String from, String to, Set<String> visited) {
    if (from.equals(to)) return true;
    if (!visited.add(from)) return false;
    return graph.getOrDefault(from, Set.of()).stream()
        .anyMatch(next -> reaches(graph, next, to, visited));
  }
}
