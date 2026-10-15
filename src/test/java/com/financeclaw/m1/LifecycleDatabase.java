package com.financeclaw.m1;

import com.financeclaw.application.TaskService;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import com.financeclaw.infrastructure.task.TaskMapper;
import javax.sql.DataSource;
import org.apache.ibatis.session.Configuration;
import org.apache.ibatis.session.LocalCacheScope;
import org.mybatis.spring.*;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;

public final class LifecycleDatabase {
  public final SqlSessionTemplate session;
  public final TaskService service;
  public final com.financeclaw.application.GovernanceService governance;

  public LifecycleDatabase(DataSource source) {
    try {
      Configuration config = new Configuration();
      config.setMapUnderscoreToCamelCase(true);
      config.setCacheEnabled(false);
      config.setLocalCacheScope(LocalCacheScope.STATEMENT);
      config.addMapper(RuntimeMapper.class);
      config.addMapper(TaskMapper.class);
      config.addMapper(com.financeclaw.infrastructure.governance.GovernanceMapper.class);
      config.addMapper(LifecycleProbeMapper.class);
      SqlSessionFactoryBean factory = new SqlSessionFactoryBean();
      factory.setDataSource(source);
      factory.setConfiguration(config);
      session = new SqlSessionTemplate(factory.getObject());
      service =
          new TaskService(
              session.getMapper(TaskMapper.class),
              session.getMapper(RuntimeMapper.class),
              new TransactionTemplate(new DataSourceTransactionManager(source)),
              session.getMapper(com.financeclaw.infrastructure.governance.GovernanceMapper.class),
              new com.financeclaw.application.ReleaseCatalog());
      governance =
          new com.financeclaw.application.GovernanceService(
              service,
              session.getMapper(TaskMapper.class),
              session.getMapper(com.financeclaw.infrastructure.governance.GovernanceMapper.class),
              session.getMapper(RuntimeMapper.class),
              new TransactionTemplate(new DataSourceTransactionManager(source)));
    } catch (Exception e) {
      throw new IllegalStateException(e);
    }
  }
}
