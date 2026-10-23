package com.financeclaw.infrastructure.agentscope.skills;

import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.agent.CapabilityBinding;
import com.financeclaw.support.*;
import io.agentscope.core.skill.AgentSkill;
import io.agentscope.core.skill.repository.*;
import java.util.*;

/** AgentScope owns discovery; only its packaged asset fingerprint enters release identity. */
public final class PackagedSkills {
    private PackagedSkills() {}

    public static final String SKILLS = "skills";

    /** Release fingerprint only. Loading, discovery and activation belong to AgentScope. */
    public static String hash() {
        try (var repository =
                new io.agentscope.core.skill.repository.ClasspathSkillRepository(SKILLS)) {
            var assets = new TreeMap<String, Object>();
            for (var skill : repository.getAllSkills()) {
                assets.put(
                        skill.getName(),
                        List.of(
                                skill.getMetadata(),
                                skill.getSkillContent(),
                                skill.getResources()));
            }
            return PayloadDigest.sha256(PayloadJson.canonical(assets));
        } catch (java.io.IOException e) {
            throw new IllegalArgumentException("Cannot load packaged skills", e);
        }
    }

    /** Individual assets are frozen for composed agents; the legacy aggregate hash is unchanged. */
    public static List<CapabilityBinding> bindings() {
        return snapshot().stream().map(PackagedSkills::binding).toList();
    }

    private static CapabilityBinding binding(AgentSkill skill) {
        String contract =
                PayloadJson.canonical(
                        Map.of(
                                "metadata",
                                skill.getMetadata(),
                                "content",
                                skill.getSkillContent(),
                                "resources",
                                skill.getResources(),
                                "source",
                                skill.getSource(),
                                "skillId",
                                skill.getSkillId()));
        return new CapabilityBinding(
                "skill:" + skill.getName(),
                "skill",
                skill.getName(),
                List.of("skill:use"),
                List.of("function:read_artifact_range"),
                contract,
                PayloadDigest.sha256(contract));
    }

    private static List<AgentSkill> snapshot() {
        try (var repository = new ClasspathSkillRepository(SKILLS)) {
            return repository.getAllSkills().stream()
                    .map(
                            skill ->
                                    new AgentSkill(
                                            skill.getMetadata(),
                                            skill.getSkillContent(),
                                            skill.getResources(),
                                            skill.getSource()))
                    .sorted(Comparator.comparing(AgentSkill::getName))
                    .toList();
        } catch (java.io.IOException e) {
            throw new Blocked("skill_release_changed");
        }
    }

    /**
     * Only selected, verified snapshots reach the SDK; no origin directory permits fallback reads.
     */
    public static AgentSkillRepository selected(List<CapabilityBinding> bindings) {
        var requested = new TreeMap<String, CapabilityBinding>();
        bindings.stream()
                .filter(binding -> binding.kind().equals("skill"))
                .forEach(binding -> requested.put(binding.name(), binding));
        var selected = new TreeMap<String, AgentSkill>();
        for (var skill : snapshot()) {
            var expected = requested.get(skill.getName());
            if (expected == null) continue;
            if (!binding(skill).equals(expected)) throw new Blocked("skill_release_changed");
            selected.put(skill.getName(), skill);
        }
        if (!selected.keySet().equals(requested.keySet()))
            throw new Blocked("skill_release_changed");
        return new SelectedRepository(selected);
    }

    private static final class SelectedRepository implements AgentSkillRepository {
        private final Map<String, AgentSkill> skills;

        SelectedRepository(Map<String, AgentSkill> skills) {
            this.skills = Map.copyOf(skills);
        }

        public AgentSkill getSkill(String name) {
            return skills.get(name);
        }

        public List<String> getAllSkillNames() {
            return skills.keySet().stream().sorted().toList();
        }

        public List<AgentSkill> getAllSkills() {
            return getAllSkillNames().stream().map(skills::get).toList();
        }

        public boolean skillExists(String name) {
            return skills.containsKey(name);
        }

        public String getSource() {
            return "classpath-" + SKILLS;
        }

        public AgentSkillRepositoryInfo getRepositoryInfo() {
            return new AgentSkillRepositoryInfo("release-snapshot", getSource(), false);
        }

        public boolean save(List<AgentSkill> values, boolean overwrite) {
            throw new UnsupportedOperationException("Read-only release");
        }

        public boolean delete(String name) {
            throw new UnsupportedOperationException("Read-only release");
        }

        public void setWriteable(boolean writable) {
            if (writable) throw new UnsupportedOperationException("Read-only release");
        }

        public boolean isWriteable() {
            return false;
        }
    }
}
