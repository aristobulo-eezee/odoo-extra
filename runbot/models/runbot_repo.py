# -*- encoding: utf-8 -*-
from openerp import models, fields, api, _
from openerp.addons.runbot.tools.helpers import (
    fqdn, run, decode_utf, mkdirs, dt2time)
from openerp.tools import config

import re
import os
import logging
import subprocess
import requests
import simplejson
import dateutil
import datetime
import signal
from datetime import time

_logger = logging.getLogger(__name__)


class RunbotRepo(models.Model):
    _name = "runbot.repo"
    _order = 'id'

    # Fields
    name = fields.Char('Repository', required=True)
    path = fields.Char(compute='_get_path', string='Directory', readonly=1)
    base = fields.Char(compute='_get_base', string='Base URL', readonly=1)
    nginx = fields.Boolean('Nginx')
    mode = fields.Selection([
        ('disabled', _('Dont check for new build')),
        ('poll', _('Poll git repository')),
        ('hook', _('Hook'))],
        string='Mode', required=True, default='poll',
        help="hook: Wait for webhook on /runbot/hook/<id> "
             "i.e. github push event")
    hook_time = fields.Datetime('Last hook time')
    duplicate_id = fields.Many2one('runbot.repo', string='Duplicate repo',
                                   help='Repository for finding duplicate '
                                        'builds')
    modules = fields.Char("Modules to install",
                          help="Comma-separated list of modules to install "
                               "and test.")
    modules_auto = fields.Selection([
        ('none', 'None (only explicit modules list)'),
        ('repo', 'Repository modules (excluding dependencies)'),
        ('all', 'All modules (including dependencies)')],
        string="Other modules to install automatically", default='repo')
    dependency_ids = fields.Many2many(
        'runbot.repo', 'runbot_repo_dep_rel',
        id1='dependant_id', id2='dependency_id',
        string='Extra dependencies',
        help="Community addon repos which need to be present to run tests.")
    token = fields.Char("Github token")
    group_ids = fields.Many2many('res.groups', string='Limited to groups')
    branch_ids = fields.One2many('runbot.branch', 'repo_id', string='Branches')

    # Methods
    @api.depends('name')
    def _get_path(self):
        root = self.root()
        for rec in self:
            name = rec.name
            for i in '@:/':
                name = name.replace(i, '_')
            rec.path = os.path.join(root, 'repo', name)

    @api.depends('name')
    def _get_base(self):
        for rec in self:
            name = re.sub('.+@', '', rec.name)
            name = re.sub('.git$', '', name)
            name = name.replace(':', '/')
            rec.base = name

    @api.model
    def domain(self):
        domain = self.env['ir.config_parameter'].sudo().get_param(
            'runbot.domain', fqdn())
        return domain

    @api.model
    def root(self):
        """Return root directory of repository"""
        default = os.path.join(os.path.dirname(__file__), 'static')
        return self.env['ir.config_parameter'].get_param('runbot.root',
                                                         default)

    @api.multi
    def git(self, cmd):
        """Execute git command cmd"""
        for repo in self:
            cmd = ['git', '--git-dir=%s' % repo.path] + cmd
            _logger.info("git: %s", ' '.join(cmd))
            return subprocess.check_output(cmd)

    @api.multi
    def git_export(self, treeish, dest):
        for repo in self:
            _logger.debug('checkout %s %s %s', repo.name, treeish, dest)
            p1 = subprocess.Popen([
                'git', '--git-dir=%s' % repo.path, 'archive', treeish],
                stdout=subprocess.PIPE)
            p2 = subprocess.Popen([
                'tar', '-xC', dest], stdin=p1.stdout, stdout=subprocess.PIPE)
            p1.stdout.close()  # Allow p1 to receive a SIGPIPE if p2 exits.
            p2.communicate()[0]

    @api.multi
    def github(self, url, payload=None, ignore_errors=False):
        """Return a http request to be sent to github"""
        for repo in self:
            if not repo.token:
                return
            try:
                match_object = re.search('([^/]+)/([^/]+)/([^/.]+(.git)?)',
                                         repo.base)
                if match_object:
                    url = url.replace(':owner', match_object.group(2))
                    url = url.replace(':repo', match_object.group(3))
                    url = 'https://api.%s%s' % (match_object.group(1), url)
                    session = requests.Session()
                    session.auth = (repo.token, 'x-oauth-basic')
                    session.headers.update({
                        'Accept': 'application/'
                                  'vnd.github.she-hulk-preview+json'})
                    if payload:
                        response = session.post(url,
                                                data=simplejson.dumps(payload))
                    else:
                        response = session.get(url)
                    response.raise_for_status()
                    return response.json()
            except Exception as e:
                _logger.debug(e)
                if ignore_errors:
                    _logger.exception('Ignored github error %s %r', url,
                                      payload)
                else:
                    # TODO: raise what?
                    raise

    @api.multi
    def update(self):
        for repo in self:
            repo.update_git()

    @api.multi
    def update_git(self):
        self.ensure_one()
        _logger.debug('repo %s updating branches', self.name)

        Build = self.env['runbot.build']
        Branch = self.env['runbot.branch']

        if not os.path.isdir(os.path.join(self.path)):
            os.makedirs(self.path)
        if not os.path.isdir(os.path.join(self.path, 'refs')):
            run(['git', 'clone', '--bare', self.name, self.path])

        # check for mode == hook
        fname_fetch_head = os.path.join(self.path, 'FETCH_HEAD')
        if os.path.isfile(fname_fetch_head):
            fetch_time = os.path.getmtime(fname_fetch_head)
            if self.mode == 'hook' and self.hook_time and \
                    dt2time(self.hook_time) < fetch_time:
                t0 = time.time()
                _logger.debug('repo %s skip hook fetch fetch_time: %ss ago '
                              'hook_time: %ss ago',
                              self.name, int(t0 - fetch_time),
                              int(t0 - dt2time(self.hook_time)))
                return

        self.git(['gc', '--auto', '--prune=all'])
        self.git(['fetch', '-p', 'origin', '+refs/heads/*:refs/heads/*'])
        self.git(['fetch', '-p', 'origin', '+refs/pull/*/head:refs/pull/*'])

        fields_name = [
            'refname', 'objectname', 'committerdate:iso8601', 'authorname',
            'authoremail', 'subject', 'committername', 'committeremail', ]

        fmt = "%00".join(["%("+field+")" for field in fields_name])
        git_refs = self.git([
            'for-each-ref', '--format', fmt, '--sort=-committerdate',
            'refs/heads', 'refs/pull'])
        git_refs = git_refs.strip()

        refs = [[decode_utf(field) for field in line.split('\x00')]
                for line in git_refs.split('\n')]

        for name, sha, date, author, author_email, subject, \
                committer, committer_email in refs:
            # create or get branch
            branch = self.branch_ids.filtered(lambda rec: rec.name == name)
            if not branch:
                _logger.debug('repo %s found new branch %s', self.name, name)
                branch = Branch.create({
                    'repo_id': self.id, 'name': name})
            # skip build for old branches
            # TODO: Move 30 days to a parameter
            if dateutil.parser.parse(date[:19]) + datetime.timedelta(30) \
                    < datetime.datetime.now():
                continue
            # create build (and mark previous builds as skipped) if not found
            builds = Build.search([('name', '=', 'sha'),
                                   ('repo_id', '=', self.id)])
            if not builds:
                _logger.debug('repo %s branch %s new build found revno %s',
                              branch.repo_id.name, branch.name, sha)
                build_info = {
                    'branch_id': branch.id,
                    'name': sha,
                    'author': author,
                    'author_email': author_email,
                    'committer': committer,
                    'committer_email': committer_email,
                    'subject': subject,
                    'date': dateutil.parser.parse(date[:19]),
                }

                if not branch.sticky:
                    builds_to_skip = Build.search([
                        ('state', '=', 'pending'),
                        ('repo_id', '=', self.id)], order='sequence asc')
                    if builds_to_skip:
                        build_info.update(sequence=builds_to_skip[0].sequence)
                    builds_to_skip.skip()
                Build.create(build_info)

        # skip old builds (if their sequence number is too low, they will
        # never be built)
        skippable_domain = [
            ('repo_id', '=', self.id),
            ('state', '=', 'pending')]
        ConfigParam = self.env['ir.config_parameter']
        running_max = int(ConfigParam.get_param('runbot.running_max',
                                                default=75))
        to_be_skipped_builds = Build.search(
            skippable_domain, order='sequence desc', offset=running_max)
        to_be_skipped_builds.skip()

    @api.multi
    def scheduler(self):
        ConfigParam = self.env['ir.config_parameter']
        workers = int(ConfigParam.get_param('runbot.workers', default=6))
        running_max = int(ConfigParam.get_param('runbot.running_max',
                                                default=75))
        host = fqdn()

        Build = self.env['runbot.build']
        domain = [('repo_id', 'in', self.ids)]
        domain_host = domain + [('host', '=', host)]

        # schedule jobs (transitions testing -> running, kill jobs, ...)
        builds = Build.search(domain_host + [('state', 'in', ['testing',
                                                              'running'])])
        builds.schedule()

        # launch new tests
        testing = Build.search_count(domain_host + [('state', '=', 'testing')])
        pending = Build.search_count(domain + [('state', '=', 'pending')])

        while testing < workers and pending > 0:
            # find sticky pending build if any, otherwise, last pending
            # (by id, not by sequence) will do the job
            pending_build = Build.search(domain + [
                ('state', '=', 'pending'),
                ('branch_id.sticky', '=', True)], limit=1)
            if not pending_build:
                pending_build = Build.search(domain + [
                    ('state', '=', 'pending')], order="sequence", limit=1)
            pending_build.schedule()

            # compute the number of testing and pending jobs again
            testing = Build.search_count(domain_host + [
                ('state', '=', 'testing')])
            pending = Build.search_count(domain + [('state', '=', 'pending')])

        # terminate and reap doomed build
        builds = Build.search(domain_host + [('state', '=', 'running')])
        # sort builds: the last build of each sticky branch then the rest
        sticky = builds.filtered(lambda b: b.sticky is True).sorted(
            key=lambda r: r.id, reverse=True)
        non_sticky = (builds - sticky).sorted(key=lambda r: r.id, reverse=True)
        # terminate extra running builds
        (sticky+non_sticky)[running_max:].kill()
        (sticky+non_sticky).reap()

    @api.model
    def reload_nginx(self):
        Build = self.env['runbot.build']
        settings = {
            'port': config['xmlrpc_port']
        }
        nginx_dir = os.path.join(self.root(), 'nginx')
        settings['nginx_dir'] = nginx_dir
        repos = self.search([('nginx', '=', True)], order='id')
        if repos:
            builds = Build.search([
                ('repo_id', 'in', [r.id for r in repos]),
                ('state', '=', 'running')])
            settings['builds'] = builds

            nginx_config = self.env['ir.ui.view'].render('runbot.nginx_config',
                                                         settings)
            mkdirs([nginx_dir])
            open(os.path.join(nginx_dir,
                              'nginx.conf'), 'w').write(nginx_config)
            try:
                _logger.debug('reload nginx')
                pid = int(open(os.path.join(nginx_dir,
                                            'nginx.pid')).read().strip(' \n'))
                os.kill(pid, signal.SIGHUP)
            except Exception as e:
                _logger.exception(e)
                _logger.debug('start nginx')
                run(['/usr/sbin/nginx', '-p', nginx_dir, '-c', 'nginx.conf'])

    @api.multi
    def killall(self):
        # kill switch
        Build = self.env['runbot.build']
        builds = Build.search([('state', 'not in', ['done', 'pending'])])
        builds.kill()

    @api.model
    def cron(self):
        repos = self.search([('mode', '!=', 'disabled')])
        repos.update()
        repos.scheduler()
        self.reload_nginx()

