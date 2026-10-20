package com.financeclaw.api;

import com.financeclaw.domain.Task.Problem;
import java.util.Map;
import org.springframework.http.ResponseEntity;
import org.springframework.http.converter.HttpMessageNotReadableException;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.method.annotation.MethodArgumentTypeMismatchException;

@RestControllerAdvice
public class ApiErrors {
  @ExceptionHandler(Problem.class)
  ResponseEntity<?> problem(Problem p) {
    return ResponseEntity.status(p.status()).body(Map.of("error", p.getMessage()));
  }

  @ExceptionHandler({
    HttpMessageNotReadableException.class,
    MethodArgumentTypeMismatchException.class
  })
  ResponseEntity<?> invalid(Exception e) {
    return ResponseEntity.unprocessableEntity().body(Map.of("error", "invalid_request"));
  }

  @ExceptionHandler(MethodArgumentNotValidException.class)
  ResponseEntity<?> invalidBody(MethodArgumentNotValidException e) {
    var type = e.getParameter().getParameterType();
    String code = "invalid_body";
    if (type == MemoryController.Decision.class) code = "invalid_memory_decision";
    else if (type == MemoryController.Forget.class) code = "invalid_memory_epoch";
    return ResponseEntity.unprocessableEntity().body(Map.of("error", code));
  }

  @ExceptionHandler(Exception.class)
  ResponseEntity<?> unavailable(Exception e) {
    return ResponseEntity.status(503).body(Map.of("error", "service_unavailable"));
  }
}
