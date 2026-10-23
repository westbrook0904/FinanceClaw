package com.financeclaw.api;

import com.financeclaw.app.agent.AgentService;
import jakarta.servlet.http.HttpServletRequest;
import java.util.List;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/** 调用者只读目录；管理操作保留在独立 CLI，不将会话 Bearer 权限升级为平台管理权限。 */
@RestController
@RequestMapping("/v1/agents")
public class AgentController {
    private final AgentService agents;

    public AgentController(AgentService agents) {
        this.agents = agents;
    }

    public record Entry(String agentId, long draftRevision, boolean builtin) {}

    @GetMapping
    public List<Entry> list(HttpServletRequest request) {
        return agents.list(TaskController.owner(request)).stream()
                .map(
                        agent ->
                                new Entry(
                                        agent.id(),
                                        agent.draftRevision(),
                                        "finance_agent".equals(agent.id())))
                .toList();
    }
}
