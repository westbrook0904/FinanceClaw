package com.financeclaw.bootstrap;

import com.financeclaw.infrastructure.agent.AgentMapper;
import com.financeclaw.infrastructure.agent.ResourceAccessMapper;
import com.financeclaw.infrastructure.channel.FeishuMapper;
import com.financeclaw.infrastructure.governance.GovernanceMapper;
import com.financeclaw.infrastructure.memory.MemoryMapper;
import com.financeclaw.infrastructure.release.ReleaseMapper;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import com.financeclaw.infrastructure.task.*;
import org.apache.ibatis.session.SqlSessionFactory;
import org.mybatis.spring.mapper.MapperFactoryBean;
import org.springframework.context.annotation.*;

/**
 * 按职责显式注册 Mapper，保持受限记忆进程与主业务库权限边界。
 *
 * <p>配置只选择接口，不执行建表或数据修复；数据库初始化由独立的一次性入口完成。
 */
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
    public static class Releases {
        @Bean
        MapperFactoryBean<ReleaseMapper> releaseMapper(SqlSessionFactory sessions) {
            return mapper(ReleaseMapper.class, sessions);
        }
    }

    @Configuration(proxyBeanMethods = false)
    public static class Agents {
        @Bean
        MapperFactoryBean<ResourceAccessMapper> resourceAccessMapper(SqlSessionFactory sessions) {
            return mapper(ResourceAccessMapper.class, sessions);
        }

        @Bean
        MapperFactoryBean<AgentMapper> agentMapper(SqlSessionFactory sessions) {
            return mapper(AgentMapper.class, sessions);
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
