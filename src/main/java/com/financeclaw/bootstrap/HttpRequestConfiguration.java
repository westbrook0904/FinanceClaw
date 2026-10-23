package com.financeclaw.bootstrap;

import com.fasterxml.jackson.core.JsonParser;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.MapperFeature;
import com.fasterxml.jackson.databind.cfg.CoercionAction;
import com.fasterxml.jackson.databind.cfg.CoercionInputShape;
import com.fasterxml.jackson.databind.type.LogicalType;
import java.util.List;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.converter.HttpMessageConverter;
import org.springframework.http.converter.json.MappingJackson2HttpMessageConverter;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

/**
 * 全应用统一的 JSON 输入规则。
 *
 * <p>DTO 按声明类型绑定：未知字段、字符串转数字、数字转布尔值和小数截断都应拒绝。 解析完成后再由 Bean Validation 检查必填项和范围，应用服务只接收已经通过边界校验的参数。
 * JsonNode 仍由对应接口校验对象结构，不安装按请求类型选择的二次反序列化转换链。
 */
@Configuration(proxyBeanMethods = false)
public class HttpRequestConfiguration implements WebMvcConfigurer {
    @Override
    public void extendMessageConverters(List<HttpMessageConverter<?>> converters) {
        for (var converter : converters) {
            if (converter instanceof MappingJackson2HttpMessageConverter jackson) {
                var mapper = jackson.getObjectMapper();
                mapper.enable(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES);
                // 每个请求只允许一个无重复键的 JSON 值，避免不同解析方对同一正文产生歧义。
                mapper.enable(DeserializationFeature.FAIL_ON_TRAILING_TOKENS);
                mapper.enable(JsonParser.Feature.STRICT_DUPLICATE_DETECTION);
                mapper.disable(DeserializationFeature.ACCEPT_FLOAT_AS_INT);
                mapper.setConfig(
                        mapper.getDeserializationConfig()
                                .without(MapperFeature.ALLOW_COERCION_OF_SCALARS));
                // Jackson 对 String 另有默认转换路径，需要显式关闭，防止数字或布尔值变成消息正文。
                mapper.coercionConfigFor(LogicalType.Textual)
                        .setCoercion(CoercionInputShape.Integer, CoercionAction.Fail)
                        .setCoercion(CoercionInputShape.Float, CoercionAction.Fail)
                        .setCoercion(CoercionInputShape.Boolean, CoercionAction.Fail);
            }
        }
    }
}
