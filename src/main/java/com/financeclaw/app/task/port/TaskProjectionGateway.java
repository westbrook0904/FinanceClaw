package com.financeclaw.app.task.port;

import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;

public interface TaskProjectionGateway {
  Delivery nextDelivery();

  void project(Delivery delivery);

  void delivered(long id);
}
