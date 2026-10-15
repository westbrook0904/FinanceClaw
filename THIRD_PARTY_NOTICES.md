# Third-party notices

## lunar-java 1.7.7

The Java calendar adapter depends on `cn.6tail:lunar:1.7.7`, by 6tail, under the MIT License.
The original copyright and license are preserved in
[src/main/resources/META-INF/licenses/lunar-1.7.7.txt](src/main/resources/META-INF/licenses/lunar-1.7.7.txt)
and packaged in the application jar.

The Maven jar, source jar and POM SHA-256 values, pinned upstream commit, and source correspondence
are recorded in [ziwei-dependencies.lock.json](ziwei-dependencies.lock.json).
The dependency declares no runtime transitive dependencies. The adapter uses explicit calendar
arguments and returns application-owned immutable values, without modifying the library's global data.

Upstream: <https://github.com/6tail/lunar-java>.

## Scope

This file records the calendar dependency introduced in M6b. It is not a complete dependency inventory
or license audit of Spring Boot, AgentScope, their transitive dependencies, or the entire application.
The x-iztro-generated test fixtures are compatibility references; the application does not bundle
x-iztro, its Rust implementation, or a Python runtime. No xuan-deduct source is incorporated.
