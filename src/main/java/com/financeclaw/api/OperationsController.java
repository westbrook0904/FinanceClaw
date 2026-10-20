package com.financeclaw.api;

import com.financeclaw.api.request.ChangeAdmissionRequest;
import com.financeclaw.app.operations.AdmissionService;
import com.financeclaw.app.operations.OperationsQueryService;
import com.financeclaw.domain.Operations.*;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.validation.Valid;
import java.util.Map;
import org.springframework.web.bind.annotation.*;

@RestController
public class OperationsController {
  private final AdmissionService admissions;
  private final OperationsQueryService operations;

  public OperationsController(AdmissionService admissions, OperationsQueryService operations) {
    this.admissions = admissions;
    this.operations = operations;
  }

  @GetMapping("/v1/operations/admission")
  public Admission admission(HttpServletRequest request) {
    return admissions.admission(TaskController.owner(request));
  }

  @PutMapping("/v1/operations/admission")
  public Receipt change(
      HttpServletRequest request,
      @RequestHeader(value = "Idempotency-Key", required = false) String key,
      @Valid @RequestBody ChangeAdmissionRequest body) {
    return admissions.change(TaskController.owner(request), key, body.paused(), body.revision());
  }

  @GetMapping("/v1/operations/drain")
  public Drain drain(HttpServletRequest request) {
    return operations.drain(TaskController.owner(request));
  }

  @GetMapping("/v1/operations/drain/items")
  public BacklogPage<?> backlog(
      HttpServletRequest request,
      @RequestParam(defaultValue = "turns") String kind,
      @RequestParam(defaultValue = "") String after,
      @RequestParam(defaultValue = "50") int limit) {
    return operations.backlog(TaskController.owner(request), kind, after, limit);
  }

  @GetMapping("/v1/operations")
  public Map<String, Object> view(HttpServletRequest request) {
    return operations.view(TaskController.owner(request));
  }
}
