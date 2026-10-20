package com.financeclaw.bootstrap;

import com.financeclaw.infrastructure.channel.FeishuMapper;
import com.financeclaw.infrastructure.governance.GovernanceMapper;
import com.financeclaw.infrastructure.memory.MemoryMapper;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import com.financeclaw.infrastructure.task.*;
import org.apache.ibatis.session.SqlSessionFactory;
import org.mybatis.spring.mapper.MapperFactoryBean;
import org.springframework.context.annotation.*;

/** Explicit MyBatis factories: adding an interface cannot silently enlarge a role's mapper set. */
public final class MapperConfiguration {
  private MapperConfiguration() {}

  private static <T> MapperFactoryBean<T> mapper(Class<T> type, SqlSessionFactory sessions) {
    var bean = new MapperFactoryBean<T>(type);
    bean.setSqlSessionFactory(sessions);
    return bean;
  }

  @Configuration(proxyBeanMethods = false)
  public static class Tasks {
    @Bean
    MapperFactoryBean<TaskMapper> taskMapper(SqlSessionFactory sessions) {
      return mapper(TaskMapper.class, sessions);
    }
  }

  @Configuration(proxyBeanMethods = false)
  public static class Governance {
    @Bean
    MapperFactoryBean<GovernanceMapper> governanceMapper(SqlSessionFactory sessions) {
      return mapper(GovernanceMapper.class, sessions);
    }
  }

  @Configuration(proxyBeanMethods = false)
  public static class Runtime {
    @Bean
    MapperFactoryBean<RuntimeMapper> runtimeMapper(SqlSessionFactory sessions) {
      return mapper(RuntimeMapper.class, sessions);
    }
  }

  @Configuration(proxyBeanMethods = false)
  public static class Memory {
    @Bean
    MapperFactoryBean<MemoryMapper> memoryMapper(SqlSessionFactory sessions) {
      return mapper(MemoryMapper.class, sessions);
    }
  }

  @Configuration(proxyBeanMethods = false)
  public static class Channel {
    @Bean
    MapperFactoryBean<FeishuMapper> feishuMapper(SqlSessionFactory sessions) {
      return mapper(FeishuMapper.class, sessions);
    }
  }

  @Configuration(proxyBeanMethods = false)
  public static class Operations {
    @Bean
    MapperFactoryBean<OperationsMapper> operationsMapper(SqlSessionFactory sessions) {
      return mapper(OperationsMapper.class, sessions);
    }
  }
}
