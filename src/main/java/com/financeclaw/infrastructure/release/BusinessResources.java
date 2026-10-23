package com.financeclaw.infrastructure.release;

import java.nio.charset.StandardCharsets;
import java.nio.file.*;

/**
 * 读取部署配置指定的 UTF-8 本地文件或 classpath 资源。
 * 文件路径来自受信任的应用配置，不接受工具请求决定读取位置；文件大小及打包资源读取量受到限制，内容随后由业务配置对象校验。I/O 失败转换为不含资源正文的固定异常。
 */
public final class BusinessResources {
    private BusinessResources() {}

    public static String read(String location) {
        try {
            if (location.startsWith("classpath:")) {
                try (var in =
                        BusinessResources.class
                                .getClassLoader()
                                .getResourceAsStream(location.substring(10))) {
                    if (in == null) {
                        throw new IllegalArgumentException("Missing packaged resource");
                    }
                    return new String(in.readNBytes(2097153), StandardCharsets.UTF_8);
                }
            }
            if (Files.size(Path.of(location)) > 2097152) {
                throw new IllegalArgumentException("Resource too large");
            }
            return Files.readString(Path.of(location));
        } catch (java.io.IOException e) {
            throw new IllegalArgumentException("Cannot load business resource");
        }
    }
}
