package com.financeclaw.infrastructure.memory;

import com.financeclaw.app.memory.port.ProfileDocuments;
import java.nio.file.Path;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.env.Environment;

/** 仅业务应用和离线遗忘恢复装配目录入口；受限证据 Worker 不获得画像目录。 */
@Configuration(proxyBeanMethods = false)
public class ProfileDocumentsConfiguration {
    @Bean
    ProfileDocuments profileDocuments(Environment environment) {
        String directory = environment.getProperty("financeclaw.memory.profile-directory", "");
        return directory.isBlank()
                ? ProfileDocuments.disabled()
                : new DirectoryProfileDocuments(Path.of(directory));
    }
}
