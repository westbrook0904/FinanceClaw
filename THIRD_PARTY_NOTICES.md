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

## x-iztro 0.4.0 algorithm port

The fixed default Ziwei chart rules in `ZiweiChartEngine`, `ZiweiStars`, `ZiweiRules`, and
`ZiweiTables` are adapted to Java from x-haose/x-iztro commit
`cd14dfd8cf18dcc1ab248ba3c20c86903f5bc07f` (v0.4.0), under MIT.
The port supports only the application's fixed default convention and Chinese output.
It replaces the original calendar calls with the pinned Java calendar adapter and returns
immutable results; no Rust, Python, JavaScript, WASM, or FFI runtime is embedded.

The original copyright and MIT license are preserved in
[x-iztro-0.4.0.txt](src/main/resources/META-INF/licenses/x-iztro-0.4.0.txt).
The constant star tables are extracted from locked upstream source, not computed chart fixtures;
see [the generator](scripts/import-ziwei-tables.py) and the source fingerprints in the dependency lock.
The upstream project itself is based on SylarLong/iztro. Its MIT notice is also retained in
[iztro.txt](src/main/resources/META-INF/licenses/iztro.txt).

## Scope

This file records the calendar dependency and algorithm port introduced in M6b. It is not a complete dependency inventory
or license audit of Spring Boot, AgentScope, their transitive dependencies, or the entire application.
The x-iztro-generated test fixtures are compatibility references and are not packaged in the application.
No xuan-deduct source is incorporated.
