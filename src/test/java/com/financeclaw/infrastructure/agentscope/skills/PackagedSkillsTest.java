package com.financeclaw.infrastructure.agentscope.skills;

import static org.assertj.core.api.Assertions.*;

import java.util.List;
import org.junit.jupiter.api.Test;

class PackagedSkillsTest {
    @Test
    void selectedRepositoryContainsOnlyPinnedImmutableAssetsWithoutFilesystemFallback() {
        var bindings = PackagedSkills.bindings();
        assertThat(bindings).isNotEmpty();
        try (var selected = PackagedSkills.selected(List.of(bindings.getFirst()))) {
            assertThat(selected.getAllSkillNames()).containsExactly(bindings.getFirst().name());
            assertThat(selected.isWriteable()).isFalse();
            assertThat(selected.getAllSkills().getFirst().getOriginDir()).isEmpty();
            assertThat(selected.getSkill("unselected")).isNull();
            assertThatThrownBy(() -> selected.setWriteable(true))
                    .isInstanceOf(UnsupportedOperationException.class);
            assertThatThrownBy(() -> selected.save(List.of(), true))
                    .isInstanceOf(UnsupportedOperationException.class);
            assertThatThrownBy(() -> selected.delete(bindings.getFirst().name()))
                    .isInstanceOf(UnsupportedOperationException.class);
        }
        try (var empty = PackagedSkills.selected(List.of())) {
            assertThat(empty.getAllSkills()).isEmpty();
            assertThat(empty.skillExists(bindings.getFirst().name())).isFalse();
        }
    }
}
