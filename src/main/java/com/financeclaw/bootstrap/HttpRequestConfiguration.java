package com.financeclaw.bootstrap;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.*;
import com.fasterxml.jackson.databind.cfg.CoercionAction;
import com.fasterxml.jackson.databind.cfg.CoercionInputShape;
import com.fasterxml.jackson.databind.deser.std.StdDelegatingDeserializer;
import com.fasterxml.jackson.databind.module.SimpleModule;
import com.fasterxml.jackson.databind.type.LogicalType;
import com.fasterxml.jackson.databind.type.TypeFactory;
import com.fasterxml.jackson.databind.util.Converter;
import com.financeclaw.api.request.ChangeAdmissionRequest;
import com.financeclaw.api.request.SubmitTurnRequest;
import com.financeclaw.domain.Task.Problem;
import java.util.List;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.converter.HttpMessageConverter;
import org.springframework.http.converter.json.MappingJackson2HttpMessageConverter;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

/** Keeps the existing JSON contract while binding selected requests to validated DTOs. */
@Configuration(proxyBeanMethods = false)
public class HttpRequestConfiguration implements WebMvcConfigurer {
  @Override
  public void extendMessageConverters(List<HttpMessageConverter<?>> converters) {
    for (var converter : converters) {
      if (converter instanceof MappingJackson2HttpMessageConverter jackson) {
        var binding = jackson.getObjectMapper().copy();
        binding.enable(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES);
        binding.disable(DeserializationFeature.ACCEPT_FLOAT_AS_INT);
        binding.setConfig(
            binding.getDeserializationConfig().without(MapperFeature.ALLOW_COERCION_OF_SCALARS));
        binding
            .coercionConfigFor(LogicalType.Textual)
            .setCoercion(CoercionInputShape.Integer, CoercionAction.Fail)
            .setCoercion(CoercionInputShape.Float, CoercionAction.Fail)
            .setCoercion(CoercionInputShape.Boolean, CoercionAction.Fail);
        var module = new SimpleModule();
        register(module, binding, SubmitTurnRequest.class);
        register(module, binding, ChangeAdmissionRequest.class);
        var requests = jackson.getObjectMapper().copy().registerModule(module);
        for (var type : List.of(SubmitTurnRequest.class, ChangeAdmissionRequest.class)) {
          jackson.registerObjectMappersForType(
              type,
              mappings ->
                  jackson.getSupportedMediaTypes().forEach(media -> mappings.put(media, requests)));
        }
      }
    }
  }

  private static <T> void register(SimpleModule module, ObjectMapper binding, Class<T> type) {
    module.addDeserializer(
        type,
        new StdDelegatingDeserializer<>(
            new Converter<JsonNode, T>() {
              @Override
              public T convert(JsonNode node) {
                // Jackson reads the complete tree first, retaining syntax precedence and last keys.
                if (!node.isObject()) throw new Problem(422, "invalid_body");
                try {
                  return binding.treeToValue(node, type);
                } catch (JsonProcessingException e) {
                  throw new Problem(422, "invalid_body");
                }
              }

              @Override
              public JavaType getInputType(TypeFactory types) {
                return types.constructType(JsonNode.class);
              }

              @Override
              public JavaType getOutputType(TypeFactory types) {
                return types.constructType(type);
              }
            }));
  }
}
