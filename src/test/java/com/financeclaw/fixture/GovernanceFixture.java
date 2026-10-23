package com.financeclaw.fixture;

import com.financeclaw.app.artifact.ArtifactService;
import com.financeclaw.app.governance.GrantService;
import com.financeclaw.app.interaction.AnswerInteractionService;
import com.financeclaw.app.report.ReportService;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import java.util.List;

/**
 * Test scenario driver; all authorization, storage and transactions remain in production services.
 */
public final class GovernanceFixture {
    private final GrantService grants;
    private final ArtifactService artifacts;
    private final AnswerInteractionService answers;
    private final ReportService reports;

    public GovernanceFixture(
            GrantService grants,
            ArtifactService artifacts,
            AnswerInteractionService answers,
            ReportService reports) {
        this.grants = grants;
        this.artifacts = artifacts;
        this.answers = answers;
        this.reports = reports;
    }

    public ArtifactService artifacts() {
        return artifacts;
    }

    public ReportService reports() {
        return reports;
    }

    public Grant grant(Owner owner, String conversation) {
        return grants.grant(owner, conversation);
    }

    public Grant changeGrant(
            Owner owner, String conversation, String key, long revision, List<String> scopes) {
        return grants.changeGrant(owner, conversation, key, revision, scopes);
    }

    public List<Interaction> interactions(Owner owner, String conversation, String turn) {
        return answers.interactions(owner, conversation, turn);
    }

    public Interaction ownedInteraction(Owner owner, String id) {
        return answers.ownedInteraction(owner, id);
    }

    public AnswerReceipt answer(Owner owner, String id, String key, Answer answer) {
        return answers.answer(owner, id, key, answer);
    }

    public AnswerReceipt answer(
            Owner owner, String conversation, String turn, String id, String key, Answer answer) {
        return answers.answer(owner, conversation, turn, id, key, answer);
    }

    public Artifact archive(Claim claim, Object payload) {
        return artifacts.archive(claim, payload);
    }

    public Artifact archive(Claim claim, Object payload, String requiredScope) {
        return artifacts.archive(claim, payload, requiredScope);
    }

    public Page read(Owner owner, String id, int start, int limit) {
        return artifacts.read(owner, id, start, limit);
    }

    public Page read(Owner owner, String id, int start, int limit, String path) {
        return artifacts.read(owner, id, start, limit, path);
    }

    public String report(Claim claim, String call, String actionHash, String title) {
        return reports.report(claim, call, actionHash, title);
    }
}
