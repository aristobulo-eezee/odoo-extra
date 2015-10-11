# -*- encoding: utf-8 -*-

import glob
import logging
import operator
import os
import re
import resource
import shutil
import signal
import subprocess
import sys
import time
from dateutil.relativedelta import relativedelta

import openerp
from openerp import models, fields, api, _
from openerp.tools import config, appdirs, DEFAULT_SERVER_DATETIME_FORMAT, ustr

from openerp.addons.runbot.tools.helpers import (
    dashes, dt2time, mkdirs, grep, rfind, lock, locked, nowait, run, now,
    s2human, flatten, decode_utf, uniq_list, fqdn)

_logger = logging.getLogger(__name__)

# Runbot Const
_re_error = r'^(?:\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} \d+ (?:ERROR|CRITICAL) )|(?:Traceback \(most recent call last\):)$'
_re_warning = r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} \d+ WARNING '
_re_job = re.compile('job_\d')

# increase cron frequency from 0.016 Hz to 0.1 Hz to reduce starvation and
# improve throughput with many workers
# TODO: find a nicer way than monkey patch to accomplish this
openerp.service.server.SLEEP_INTERVAL = 10
openerp.addons.base.ir.ir_cron._intervalTypes['minutes'] = lambda interval: relativedelta(seconds=interval*10)


class RunbotBuild(models.Model):
    _name = "runbot.build"
    _order = 'id desc'

    branch_id = fields.Many2one('runbot.branch', string='Branch',
                                required=True, ondelete='cascade', select=1)
    repo_id = fields.Many2one(
        'runbot.repo', related='branch_id.repo_id', string="Repository",
        readonly=True, store=True, ondelete='cascade', select=1)
    name = fields.Char(string='Revno', required=True, select=1)
    host = fields.Char(string='Host')
    port = fields.Integer(string='Port')
    dest = fields.Char(compute='_get_dest', string='Dest', readonly=1,
                       store=True)
    domain = fields.Char(compute='_get_domain', string='URL')
    date = fields.Datetime(string='Commit date')
    author = fields.Char(string='Author')
    author_email = fields.Char(string='Author Email')
    committer = fields.Char(string='Committer')
    committer_email = fields.Char('Committer Email')
    subject = fields.Text('Subject')
    sequence = fields.Integer(string='Sequence', select=1)
    modules = fields.Char(string="Modules to Install")
    result = fields.Selection(selection=[
        ('ok', _('Ok')),
        ('ko', _('K.O')),
        ('warn', _('Warning')),
        ('skipped', _('Skipped')),
        ('killed', _('Killed'))], string='Result', default='')
    pid = fields.Integer(string='Pid')
    state = fields.Selection(selection=[
        ('pending', _('Pending')),
        ('testing', _('Testing')),
        ('running', _('Running')),
        ('done', _('Done')),
        ('duplicate', _('Duplicate'))], string='Status', default='pending')
    job = fields.Char(string='Job', help='job_*')
    job_start = fields.Datetime(string='Job start')
    job_end = fields.Datetime(string='Job end')
    job_time = fields.Integer(compute='_get_time', string='Job time')
    job_age = fields.Integer(compute='_get_age', string='Job age')
    duplicate_id = fields.Many2one('runbot.build',
                                   string='Corresponding Build')
    server_match = fields.Selection([
        ('builtin', 'This branch includes Odoo server'),
        ('exact', 'PR target or matching name prefix found'),
        ('fuzzy', 'Fuzzy - common ancestor found'),
        ('default', 'No match found - defaults to master')],
        string='Server branch matching')

    @api.depends('name', 'branch_id', 'branch_id.name')
    def _get_dest(self):
        for build in self:
            nickname = dashes(build.branch_id.name.split('/')[2])[:32]
            build.dest = "%05d-%s-%s" % (build.id, nickname, build.name[:6])

    @api.depends('job', 'job_start', 'job_end')
    def _get_time(self):
        """Return the time taken by the tests"""
        for build in self:
            if build.job_end:
                build.job_time = int(dt2time(build.job_end) -
                                     dt2time(build.job_start))
            elif build.job_start:
                build.job_time = int(time.time() - dt2time(build.job_start))

    @api.depends('job', 'job_start')
    def _get_age(self):
        """Return the time between job start and now"""
        for build in self:
            if build.job_start:
                build.job_age = int(time.time() - dt2time(build.job_start))

    @api.depends('dest', 'host', 'port', 'repo_id', 'repo_id.nginx')
    def _get_domain(self):
        domain = self.env['runbot.repo'].domain()
        for build in self:
            if build.repo_id.nginx:
                build.domain = "%s.%s" % (build.dest, build.host)
            else:
                build.domain = "%s:%s" % (domain, build.port)

    @api.model
    @api.returns('self', lambda value: value.id)
    def create(self, values):
        build = super(RunbotBuild, self).create(values)
        extra_info = {'sequence': build.id}

        # detect duplicate
        domain = [
            ('repo_id', '=', build.repo_id.duplicate_id.id),
            ('name', '=', build.name), 
            ('duplicate_id', '=', False), 
            '|', ('result', '=', False), ('result', '!=', 'skipped')
        ]
        duplicated_build = self.search(domain, limit=1)

        if len(duplicated_build):
            extra_info.update({
                'state': 'duplicate',
                'duplicate_id': duplicated_build.id})
            duplicated_build.duplicate_id = build.id
        build.write(extra_info)
        return build

    @api.multi
    def reset(self):
        self.write({'state': 'pending'})

    def logger(self, cr, uid, ids, *l, **kw):
        l = list(l)
        for build in self.browse(cr, uid, ids, **kw):
            l[0] = "%s %s" % (build.dest, l[0])
            _logger.debug(*l)

    def list_jobs(self):
        return sorted(job for job in dir(self) if _re_job.match(job))

    @api.model
    def find_port(self):
        # currently used port
        builds = self.search([('state', 'not in', ['pending', 'done'])])
        ports = set(b.port for b in builds)

        # starting port
        ConfigParam = self.env['ir.config_parameter']
        port = int(ConfigParam.get_param('runbot.starting_port', default=2000))

        # find next free port
        while port in ports:
            port += 2

        return port

    @api.multi
    def _get_closest_branch_name(self, target_repo_id):
        """Return (repo, branch name) of the closest common branch between
           build's branch and
           any branch of target_repo or its duplicated repos.

        Rules priority for choosing the branch from the other repo is:
        1. Same branch name
        2. A PR whose head name match
        3. Match a branch which is the dashed-prefix of current branch name
        4. Common ancestors (git merge-base)
        Note that PR numbers are replaced by the branch name of the PR target
        to prevent the above rules to mistakenly link PR of different repos
        together.
        """
        self.ensure_one()
        Branch = self.env['runbot.branch']

        branch, repo = self.branch_id, self.repo_id
        pi = branch._get_pull_info()
        name = pi['base']['ref'] if pi else branch.branch_name

        target_repo = self.env['runbot.repo'].browse(target_repo_id)

        target_repo_ids = [target_repo_id]
        r = target_repo.duplicate_id
        while r:
            if r.id in target_repo_ids:
                break
            target_repo_ids.append(r.id)
            r = r.duplicate_id

        sort_by_repo = lambda d: (target_repo_ids.index(d['repo_id'][0]), -1 *
                                  len(d.get('branch_name', '')), -1 * d['id'])
        result_for = lambda d: (d['repo_id'][0], d['name'], 'exact')

        # 1. same name, not a PR
        domain = [
            ('repo_id', 'in', target_repo_ids),
            ('branch_name', '=', name),
            ('name', '=like', 'refs/heads/%'),
        ]
        targets = Branch.search_read(domain, ['name', 'repo_id'],
                                     order='id DESC')
        targets = sorted(targets, key=sort_by_repo)
        if targets:
            return result_for(targets[0])

        # 2. PR with head name equals
        domain = [
            ('repo_id', 'in', target_repo_ids),
            ('pull_head_name', '=', name),
            ('name', '=like', 'refs/pull/%'),
        ]
        pulls = Branch.search_read(domain, ['name', 'repo_id'],
                                   order='id DESC')
        pulls = sorted(pulls, key=sort_by_repo)
        for pull in pulls:
            pi = Branch._get_pull_info([pull['id']])
            if pi.get('state') == 'open':
                return result_for(pull)

        # 3. Match a branch which is the dashed-prefix of current branch name
        branches = Branch.search_read([
            ('repo_id', 'in', target_repo_ids),
            ('name', '=like', 'refs/heads/%')],
            ['name', 'branch_name', 'repo_id'], order='id DESC')
        branches = sorted(branches, key=sort_by_repo)

        for branch in branches:
            if name.startswith(branch['branch_name'] + '-'):
                return result_for(branch)

        # 4. Common ancestors (git merge-base)
        for target_id in target_repo_ids:
            common_refs = {}
            self.env.cr.execute("""
                SELECT b.name
                  FROM runbot_branch b,
                       runbot_branch t
                 WHERE b.repo_id = %s
                   AND t.repo_id = %s
                   AND b.name = t.name
                   AND b.name LIKE 'refs/heads/%%'
            """, [repo.id, target_id])
            for common_name, in self.env.cr.fetchall():
                try:
                    commit = repo.git(['merge-base', branch['name'],
                                       common_name]).strip()
                    cmd = ['log', '-1', '--format=%cd', '--date=iso', commit]
                    common_refs[common_name] = repo.git(cmd).strip()
                except subprocess.CalledProcessError:
                    # If merge-base doesn't find any common ancestor, the
                    # command exits with a non-zero return code, resulting in
                    # subprocess.check_output raising this exception. We ignore
                    #  this branch as there is no common ref between us.
                    continue
            if common_refs:
                b = sorted(common_refs.iteritems(), key=operator.itemgetter(1),
                           reverse=True)[0][0]
                return target_id, b, 'fuzzy'

        # 5. last-resort value
        return target_repo_id, 'master'

    def path(self, cr, uid, ids, *l, **kw):
        for build in self.browse(cr, uid, ids, context=None):
            root = self.pool['runbot.repo'].root(cr, uid)
            return os.path.join(root, 'build', build.dest, *l)

    def server(self, cr, uid, ids, *l, **kw):
        for build in self.browse(cr, uid, ids, context=None):
            if os.path.exists(build.path('odoo')):
                return build.path('odoo', *l)
            return build.path('openerp', *l)

    @api.model
    def filter_modules(self, modules, available_modules, explicit_modules):
        blacklist_modules = set(['auth_ldap', 'document_ftp', 'base_gengo',
                                 'website_gengo', 'website_instantclick'])

        mod_filter = lambda m: (
            m in available_modules and
            (m in explicit_modules or
             (not m.startswith(('hw_', 'theme_', 'l10n_')) and
              m not in blacklist_modules))
        )
        return uniq_list(filter(mod_filter, modules))

    @api.multi
    def checkout(self):
        for build in self:
            # starts from scratch
            if os.path.isdir(build.path()):
                shutil.rmtree(build.path())

            # runbot log path
            mkdirs([build.path("logs"), build.server('addons')])

            # checkout branch
            build.branch_id.repo_id.git_export(build.name, build.path())

            # v6 rename bin -> openerp
            if os.path.isdir(build.path('bin/addons')):
                shutil.move(build.path('bin'), build.server())

            has_server = os.path.isfile(build.server('__init__.py'))
            server_match = 'builtin'

            # build complete set of modules to install
            modules_to_move = []
            modules_to_test = ((build.branch_id.modules or '') + ',' +
                               (build.repo_id.modules or ''))
            modules_to_test = filter(None, modules_to_test.split(','))
            explicit_modules = set(modules_to_test)
            _logger.debug("manual modules_to_test for build %s: %s",
                          build.dest, modules_to_test)

            if not has_server:
                if build.repo_id.modules_auto == 'repo':
                    modules_to_test += [
                        os.path.basename(os.path.dirname(a))
                        for a in glob.glob(build.path('*/__openerp__.py'))
                    ]
                    _logger.debug("local modules_to_test for build %s: %s",
                                  build.dest, modules_to_test)

                for extra_repo in build.repo_id.dependency_ids:
                    repo, closest_name, server_match = extra_repo._get_closest_branch_name()
                    repo.git_export(closest_name, build.path())

                # Finally mark all addons to move to openerp/addons
                modules_to_move += [
                    os.path.dirname(module)
                    for module in glob.glob(build.path('*/__openerp__.py'))
                ]

            # move all addons to server addons path
            for module in uniq_list(glob.glob(build.path('addons/*')) +
                                    modules_to_move):
                basename = os.path.basename(module)
                if os.path.exists(build.server('addons', basename)):
                    build._log(
                        'Building environment',
                        'You have duplicate modules in your branches "%s"' %
                        basename
                    )
                    shutil.rmtree(build.server('addons', basename))
                shutil.move(module, build.server('addons'))

            available_modules = [
                os.path.basename(os.path.dirname(a))
                for a in glob.glob(build.server('addons/*/__openerp__.py'))
            ]
            if build.repo_id.modules_auto == 'all' or \
                    (build.repo_id.modules_auto != 'none' and has_server):
                modules_to_test += available_modules

            modules_to_test = self.filter_modules(
                modules_to_test, set(available_modules), explicit_modules)
            _logger.debug("modules_to_test for build %s: %s", build.dest,
                          modules_to_test)
            build.write({
                'server_match': server_match,
                'modules': ','.join(modules_to_test)})

    def pg_dropdb(self, dbname):
        run(['dropdb', dbname])
        # cleanup filestore
        datadir = appdirs.user_data_dir()
        paths = [os.path.join(datadir, pn, 'filestore', dbname)
                 for pn in 'OpenERP Odoo'.split()]
        run(['rm', '-rf'] + paths)

    def pg_createdb(self, dbname):
        self.pg_dropdb(dbname)
        _logger.debug("createdb %s", dbname)
        run(['createdb', '--encoding=unicode', '--lc-collate=C',
             '--template=template0', dbname])

    @api.multi
    def cmd(self):
        """Return a list describing the command to start the build"""
        for build in self:
            # Server
            server_path = build.path("openerp-server")
            # for 7.0
            if not os.path.isfile(server_path):
                server_path = build.path("openerp-server.py")
            # for 6.0 branches
            if not os.path.isfile(server_path):
                server_path = build.path("bin/openerp-server.py")

            # commandline
            cmd = [
                sys.executable,
                server_path,
                "--xmlrpc-port=%d" % build.port,
            ]
            # options
            if grep(build.server("tools/config.py"), "no-xmlrpcs"):
                cmd.append("--no-xmlrpcs")
            if grep(build.server("tools/config.py"), "no-netrpc"):
                cmd.append("--no-netrpc")
            if grep(build.server("tools/config.py"), "log-db"):
                logdb = self.env.cr.dbname
                if config['db_host'] and grep(build.server('sql_db.py'),
                                              'allow_uri'):
                    logdb = 'postgres://{cfg[db_user]}:' \
                            '{cfg[db_password]}@{cfg[db_host]}/{db}'.format(
                        cfg=config, db=logdb)
                cmd += ["--log-db=%s" % logdb]

            if grep(build.server("tools/config.py"), "data-dir"):
                datadir = build.path('datadir')
                if not os.path.exists(datadir):
                    os.mkdir(datadir)
                cmd += ["--data-dir", datadir]

        # coverage
        # coverage_file_path=os.path.join(log_path,'coverage.pickle')
        # coverage_base_path=os.path.join(log_path,'coverage-base')
        # coverage_all_path=os.path.join(log_path,'coverage-all')
        # cmd = ["coverage","run","--branch"] + cmd
        # self.run_log(cmd, logfile=self.test_all_path)
        # run(["coverage","html","-d",self.coverage_base_path,
        #      "--ignore-errors","--include=*.py"],
        #     env={'COVERAGE_FILE': self.coverage_file_path})

        return cmd, build.modules

    def spawn(self, cmd, lock_path, log_path, cpu_limit=None, shell=False):
        def preexec_fn():
            os.setsid()
            if cpu_limit:
                # set soft cpulimit
                soft, hard = resource.getrlimit(resource.RLIMIT_CPU)
                r = resource.getrusage(resource.RUSAGE_SELF)
                cpu_time = r.ru_utime + r.ru_stime
                resource.setrlimit(resource.RLIMIT_CPU,
                                   (cpu_time + cpu_limit, hard))
            # close parent files
            os.closerange(3, os.sysconf("SC_OPEN_MAX"))
            lock(lock_path)
        out = open(log_path, "w")
        _logger.debug("spawn: %s stdout: %s", ' '.join(cmd), log_path)
        p = subprocess.Popen(cmd, stdout=out, stderr=out,
                             preexec_fn=preexec_fn, shell=shell)
        return p.pid

    @api.multi
    def github_status(self):
        """Notify github of failed/successful builds"""
        runbot_domain = self.env['runbot.repo'].domain()
        for build in self:
            desc = "runbot build %s" % (build.dest,)
            if build.state == 'testing':
                state = 'pending'
            elif build.state in ('running', 'done'):
                state = 'error'
                if build.result == 'ok':
                    state = 'success'
                if build.result == 'ko':
                    state = 'failure'
                desc += " (runtime %ss)" % (build.job_time,)
            else:
                continue
            status = {
                "state": state,
                "target_url": "http://%s/runbot/build/%s" % (runbot_domain,
                                                             build.id),
                "description": desc,
                "context": "ci/runbot"
            }
            _logger.debug("github updating status %s to %s", build.name, state)
            build.repo_id.github('/repos/:owner/:repo/statuses/%s' %
                                 build.name, status, ignore_errors=True)

    @api.multi
    def job_00_init(self, lock_path, log_path):
        self.ensure_one()
        self._log('init', 'Init build environment')
        # notify pending build - avoid confusing users by saying nothing
        self.github_status()
        self.checkout()
        return -2

    @api.multi
    def job_10_test_base(self, lock_path, log_path):
        self.ensure_one()
        self._log('test_base', 'Start test base module')
        # run base test
        self.pg_createdb("%s-base" % self.dest)
        cmd, mods = self.cmd()
        if grep(self.server("tools/config.py"), "test-enable"):
            cmd.append("--test-enable")
        cmd += ['-d', '%s-base' % self.dest, '-i', 'base', '--stop-after-init',
                '--log-level=test', '--max-cron-threads=0']
        return self.spawn(cmd, lock_path, log_path, cpu_limit=300)

    @api.multi
    def job_20_test_all(self, lock_path, log_path):
        self.ensure_one()
        self._log('test_all', 'Start test all modules')
        self.pg_createdb("%s-all" % self.dest)
        cmd, mods = self.cmd()
        if grep(self.server("tools/config.py"), "test-enable"):
            cmd.append("--test-enable")
        cmd += ['-d', '%s-all' % self.dest, '-i', ustr(mods),
                '--stop-after-init', '--log-level=test',
                '--max-cron-threads=0']
        # reset job_start to an accurate job_20 job_time
        self.write({'job_start': now()})
        return self.spawn(cmd, lock_path, log_path, cpu_limit=2100)

    @api.multi
    def job_30_run(self, lock_path, log_path):
        self.ensure_one()
        # adjust job_end to record an accurate job_20 job_time
        self._log('run', 'Start running build %s' % self.dest)
        log_all = self.path('logs', 'job_20_test_all.txt')
        log_time = time.localtime(os.path.getmtime(log_all))
        v = {
            'job_end': time.strftime(DEFAULT_SERVER_DATETIME_FORMAT, log_time),
        }
        if grep(log_all, ".modules.loading: Modules loaded."):
            if rfind(log_all, _re_error):
                v['result'] = "ko"
            elif rfind(log_all, _re_warning):
                v['result'] = "warn"
            elif not grep(self.server("test/common.py"), "post_install") or \
                    grep(log_all, "Initiating shutdown."):
                v['result'] = "ok"
        else:
            v['result'] = "ko"
        self.write(v)
        self.github_status()

        # run server
        cmd, mods = self.cmd()
        if os.path.exists(self.server('addons/im_livechat')):
            cmd += ["--workers", "2"]
            cmd += ["--longpolling-port", "%d" % (self.port + 1)]
            cmd += ["--max-cron-threads", "1"]
        else:
            # not sure, to avoid old server to check other dbs
            cmd += ["--max-cron-threads", "0"]

        cmd += ['-d', "%s-all" % self.dest]

        if grep(self.server("tools/config.py"), "db-filter"):
            if self.repo_id.nginx:
                cmd += ['--db-filter', '%d.*$']
            else:
                cmd += ['--db-filter', '%s.*$' % self.dest]

        ## Web60
        # self.client_web_path=os.path.join(self.running_path, "client-web")
        # self.client_web_bin_path=os.path.join(self.client_web_path,
        #                                       "openerp-web.py")
        # self.client_web_doc_path=os.path.join(self.client_web_path, "doc")
        # webclient_config % (self.client_web_port+port,
        #                     self.server_net_port+port,
        #                     self.server_net_port+port)
        # cfgs = [os.path.join(self.client_web_path, "doc", "openerp-web.cfg"),
        #         os.path.join(self.client_web_path,"openerp-web.cfg")]
        # for i in cfgs:
        #     f = open(i, "w")
        #    f.write(config)
        #    f.close()
        # cmd = [self.client_web_bin_path]

        return self.spawn(cmd, lock_path, log_path, cpu_limit=None)

    @api.multi
    def force(self):
        """Force a rebuild
        :return runbot.repo record (used by controller at /build/<id>/force)
        """
        for build in self:
            domain = [('state', '=', 'pending')]
            pending = self.search(domain, order='id', limit=1)
            if pending:
                sequence = pending.id
            else:
                sequence = self.search([], order='id desc', limit=1).id

            # Force it now
            if build.state == 'done' and build.result == 'skipped':
                values = {
                    'state': 'pending',
                    'sequence': sequence,
                    'result': ''}
                build.sudo().write(values)
            # or duplicate it
            elif build.state in ['running', 'done', 'duplicate']:
                new_build = {
                    'sequence': sequence,
                    'branch_id': build.branch_id.id,
                    'name': build.name,
                    'author': build.author,
                    'author_email': build.author_email,
                    'committer': build.committer,
                    'committer_email': build.committer_email,
                    'subject': build.subject,
                    'modules': build.modules,
                }
                self.sudo().create(new_build)
        return build.repo_id

    @api.multi
    def schedule(self):
        jobs = self.list_jobs()

        ConfigParam = self.env['ir.config_parameter']
        # For retro-compatibility, keep this parameter in seconds
        default_timeout = int(ConfigParam.get_param('runbot.timeout',
                                                    default=1800)) / 60

        for build in self:
            if build.state == 'pending':
                # allocate port and schedule first job
                port = self.find_port()
                values = {
                    'host': fqdn(),
                    'port': port,
                    'state': 'testing',
                    'job': jobs[0],
                    'job_start': now(),
                    'job_end': False,
                }
                build.write(values)
                self.env.cr.commit()
            else:
                # check if current job is finished
                lock_path = build.path('logs', '%s.lock' % build.job)
                if locked(lock_path):
                    # kill if overpassed
                    timeout = (build.branch_id.job_timeout or
                               default_timeout) * 60
                    if build.job != jobs[-1] and build.job_time > timeout:
                        build.logger('%s time exceded (%ss)', build.job,
                                     build.job_time)
                        build.kill(result='killed')
                    continue
                build.logger('%s finished', build.job)
                # schedule
                v = {}
                # testing -> running
                if build.job == jobs[-2]:
                    v['state'] = 'running'
                    v['job'] = jobs[-1]
                    v['job_end'] = now(),
                # running -> done
                elif build.job == jobs[-1]:
                    v['state'] = 'done'
                    v['job'] = ''
                # testing
                else:
                    v['job'] = jobs[jobs.index(build.job) + 1]
                build.write(v)
            build.refresh()

            # run job
            pid = None
            if build.state != 'done':
                build.logger('running %s', build.job)
                job_method = getattr(self, build.job)
                mkdirs([build.path('logs')])
                lock_path = build.path('logs', '%s.lock' % build.job)
                log_path = build.path('logs', '%s.txt' % build.job)
                pid = job_method(build, lock_path, log_path)
                build.write({'pid': pid})
            # needed to prevent losing pids if multiple jobs are started
            # and one them raise an exception
            self.env.cr.commit()

            if pid == -2:
                # no process to wait, directly call next job
                # FIXME find a better way that this recursive call
                build.schedule()

            # cleanup only needed if it was not killed
            if build.state == 'done':
                build.cleanup()

    @api.multi
    def skip(self):
        self.write({'state': 'done', 'result': 'skipped'})
        to_unduplicate = self.filtered(lambda b: b.duplicate_id is True)
        to_unduplicate.force()

    @api.multi
    def cleanup(self):
        for build in self:
            self.env.cr.execute("""
                SELECT datname
                  FROM pg_database
                 WHERE pg_get_userbyid(datdba) = current_user
                   AND datname LIKE %s
            """, [build.dest + '%'])
            for db, in self.env.cr.fetchall():
                self.pg_dropdb(db)

            if os.path.isdir(build.path()) and build.result != 'killed':
                shutil.rmtree(build.path())

    @api.multi
    def kill(self, result=None):
        for build in self:
            build._log('kill', 'Kill build %s' % build.dest)
            build.logger('killing %s', build.pid)
            try:
                os.killpg(build.pid, signal.SIGKILL)
            except OSError:
                pass
            v = {'state': 'done', 'job': False}
            if result:
                v['result'] = result
            build.write(v)
            self.env.cr.commit()   # Really?
            build.github_status()
            build.cleanup()

    def reap(self):
        while True:
            try:
                pid, status, rusage = os.wait3(os.WNOHANG)
            except OSError:
                break
            if pid == 0:
                break
            _logger.debug('reaping: pid: %s status: %s', pid, status)

    @api.multi
    def _log(self, func, message):
        self.ensure_one()
        _logger.debug("Build %s %s %s", self.id, func, message)
        self.env['ir.logging'].create({
            'build_id': self.id,
            'level': 'INFO',
            'type': 'runbot',
            'name': 'odoo.runbot',
            'message': message,
            'path': 'runbot',
            'func': func,
            'line': '0',
        })
