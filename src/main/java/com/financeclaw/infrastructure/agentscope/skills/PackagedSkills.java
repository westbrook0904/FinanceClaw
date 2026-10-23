package com.financeclaw.infrastructure.agentscope.skills;

import com.financeclaw.support.*;
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
}
