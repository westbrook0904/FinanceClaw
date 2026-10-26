package com.financeclaw.app.execution.port;

import com.financeclaw.domain.Task.Command;
import com.financeclaw.domain.Task.Turn;

/**
 * Optional persistent scheduling policy; selection and accounting join the normal claim
 * transaction.
 */
public interface CommandDispatch {
    Command next(String worker);

    void claimed(Command command, Turn turn, String worker);
}
