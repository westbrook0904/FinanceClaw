package com.financeclaw.api;

import com.financeclaw.app.artifact.ArtifactService;
import com.financeclaw.support.ArtifactPayload;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.nio.charset.StandardCharsets;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

/**
 * 归档 JSON 文件的范围读取和下载入口。认证主体由服务端上下文提供，应用服务在读取事务内重新验证 owner、授权和遗忘状态。 所有正文来自产物存储，不接受本地路径；下载强制
 * attachment 和 no-store，防止浏览器执行或缓存工具输出。
 */
@RestController
@RequestMapping("/v1/artifacts/{artifact}")
public class ArtifactContentController {
    private final ArtifactService artifacts;

    public ArtifactContentController(ArtifactService artifacts) {
        this.artifacts = artifacts;
    }

    @GetMapping("/range")
    public ArtifactPayload.Range range(
            HttpServletRequest request,
            HttpServletResponse response,
            @PathVariable String artifact,
            @RequestParam(defaultValue = "0") int start,
            @RequestParam(defaultValue = "4096") int maxBytes) {
        response.setHeader("Cache-Control", "no-store");
        response.setHeader("X-Content-Type-Options", "nosniff");
        return artifacts.readRange(TaskController.owner(request), artifact, start, maxBytes);
    }

    @GetMapping("/download")
    public ResponseEntity<byte[]> download(
            HttpServletRequest request,
            HttpServletResponse response,
            @PathVariable String artifact) {
        response.setHeader("Cache-Control", "no-store");
        var document = artifacts.download(TaskController.owner(request), artifact);
        byte[] content = document.content();
        return ResponseEntity.ok()
                .contentType(new MediaType("application", "json", StandardCharsets.UTF_8))
                .header("Content-Disposition", "attachment; filename=\"tool-result.json\"")
                .header("Cache-Control", "no-store")
                .header("X-Content-Type-Options", "nosniff")
                .header("X-Artifact-SHA256", document.hash())
                .contentLength(content.length)
                .body(content);
    }
}
