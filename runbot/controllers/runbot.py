# -*- encoding: utf-8 -*-

from openerp import http
from openerp.http import request
from openerp.fields import Datetime
from openerp.addons.website.models.website import slug
from openerp.addons.website_sale.controllers.main import QueryURL
from openerp.addons.runbot.tools.helpers import flatten, uniq_list, s2human

import operator
import hashlib
import werkzeug
from matplotlib.font_manager import FontProperties
from matplotlib.textpath import TextToPath
from collections import OrderedDict


class RunbotController(http.Controller):
    @http.route([
        '/runbot', '/runbot/repo/<model("runbot.repo"):repo>'], type='http',
        auth="public", website=True)
    def repo(self, repo=None, search='', limit='100', refresh='', **post):
        env, cr = request.env, request.cr
        Branch = env['runbot.branch']
        Build = env['runbot.build']
        Repo = env['runbot.repo']

        count = lambda dom: Build.search_count(dom)

        repos = Repo.search([])
        if not repo and repos:
            repo = repos[0]

        context = {
            'repos': repos,
            'repo': repo,
            'host_stats': [],
            'pending_total': count([('state', '=', 'pending')]),
            'limit': limit,
            'search': search,
            'refresh': refresh,
        }

        if repo:
            filters = {
                key: post.get(key, '1') for key in ['pending', 'testing',
                                                    'running', 'done']}
            domain = [('repo_id', '=', repo.id)]
            domain += [('state', '!=', key)
                       for key, value in filters.iteritems() if value == '0']
            if search:
                domain += ['|',
                           '|',
                           ('dest', 'ilike', search),
                           ('subject', 'ilike', search),
                           ('branch_id.branch_name', 'ilike', search)]

            build_ids = Build.search(domain, limit=int(limit))
            build_ids = [b.id for b in build_ids]
            branch_ids, build_by_branch_ids = [], {}

            if build_ids:
                branch_query = """
                SELECT br.id FROM runbot_branch br INNER JOIN runbot_build bu
                ON br.id=bu.branch_id WHERE bu.id in %s
                ORDER BY bu.sequence DESC
                """
                sticky_dom = [('repo_id', '=', repo.id), ('sticky', '=', True)]
                sticky_branch_ids = [] if search else \
                    [b.id for b in Branch.search(sticky_dom)]
                cr.execute(branch_query, (tuple(build_ids),))
                branch_ids = uniq_list(sticky_branch_ids +
                                       [br[0] for br in cr.fetchall()])

                build_query = """
                    SELECT
                        branch_id,
                        max(case when br_bu.row = 1 then br_bu.build_id end),
                        max(case when br_bu.row = 2 then br_bu.build_id end),
                        max(case when br_bu.row = 3 then br_bu.build_id end),
                        max(case when br_bu.row = 4 then br_bu.build_id end)
                    FROM (
                        SELECT
                            br.id AS branch_id,
                            bu.id AS build_id,
                            row_number() OVER (PARTITION BY branch_id) AS row
                        FROM
                            runbot_branch br INNER JOIN runbot_build bu
                            ON br.id=bu.branch_id
                        WHERE
                            br.id in %s
                        GROUP BY br.id, bu.id
                        ORDER BY br.id, bu.id DESC
                    ) AS br_bu
                    WHERE
                        row <= 4
                    GROUP BY br_bu.branch_id;
                """
                cr.execute(build_query, (tuple(branch_ids),))
                build_by_branch_ids = {
                    rec[0]: [r for r in rec[1:] if r is not None]
                    for rec in cr.fetchall()
                }

            branches = Branch.browse(branch_ids)
            build_ids = flatten(build_by_branch_ids.values())
            build_dict = {
                build.id: build for build in Build.browse(build_ids)
            }

            def branch_info(branch):
                return {
                    'branch': branch,
                    'builds': [self.build_info(build_dict[build_id])
                               for build_id in build_by_branch_ids[branch.id]]
                }

            context.update({
                'branches': [branch_info(b) for b in branches],
                'testing': count([('repo_id', '=', repo.id),
                                  ('state', '=', 'testing')]),
                'running': count([('repo_id', '=', repo.id),
                                  ('state', '=', 'running')]),
                'pending': count([('repo_id', '=', repo.id),
                                  ('state', '=', 'pending')]),
                'qu': QueryURL('/runbot/repo/'+slug(repo), search=search,
                               limit=limit, refresh=refresh, **filters),
                'filters': filters,
            })

        for result in Build.read_group([], ['host'], ['host']):
            if result['host']:
                context['host_stats'].append({
                    'host': result['host'],
                    'testing': count([('state', '=', 'testing'),
                                      ('host', '=', result['host'])]),
                    'running': count([('state', '=', 'running'),
                                      ('host', '=', result['host'])]),
                })

        return request.render("runbot.repo", context)

    @http.route(['/runbot/hook/<repo_id:int>'],
                type='http', auth="public", website=True)
    def repo(self, repo_id=None, **post):
        # TODO: if repo_id == None parse the json['repository']['ssh_url']
        # and find the right repo
        repo = request.env['runbot.repo'].sudo().browse(repo_id)
        repo.hook_time = Datetime.now()
        return ""

    @http.route(['/runbot/dashboard'],
                type='http', auth="public", website=True)
    def dashboard(self, refresh=None):
        env, cr = request.env, request.cr
        Build = env['runbot.build']
        repos = env['runbot.repo'].search([])   # respect record rules

        cr.execute("""SELECT bu.id
                        FROM runbot_branch br
                        JOIN LATERAL (SELECT *
                                        FROM runbot_build bu
                                       WHERE bu.branch_id = br.id
                                    ORDER BY id DESC
                                       LIMIT 3
                                     ) bu ON (true)
                       WHERE br.sticky
                         AND br.repo_id in %s
                    ORDER BY br.repo_id, br.branch_name, bu.id DESC
                   """, [tuple(repos.ids())])

        builds = Build.browse(map(operator.itemgetter(0), cr.fetchall()))

        count = Build.search_count
        qctx = {
            'refresh': refresh,
            'host_stats': [],
            'pending_total': count([('state', '=', 'pending')]),
        }

        repos_values = qctx['repo_dict'] = OrderedDict()
        for build in builds:
            repo = build.repo_id
            branch = build.branch_id
            r = repos_values.setdefault(repo.id, {'branches': OrderedDict()})
            if 'name' not in r:
                r.update({
                    'name': repo.name,
                    'base': repo.base,
                    'testing': count([
                        ('repo_id', '=', repo.id), ('state', '=', 'testing')]),
                    'running': count([
                        ('repo_id', '=', repo.id), ('state', '=', 'running')]),
                    'pending': count([
                        ('repo_id', '=', repo.id), ('state', '=', 'pending')]),
                })
            b = r['branches'].setdefault(branch.id, {
                'name': branch.branch_name, 'builds': list()})
            b['builds'].append(self.build_info(build))

        for result in Build.read_group([], ['host'], ['host']):
            if result['host']:
                qctx['host_stats'].append({
                    'host': result['host'],
                    'testing': count([
                        ('state', '=', 'testing'),
                        ('host', '=', result['host'])]),
                    'running': count([
                        ('state', '=', 'running'),
                        ('host', '=', result['host'])]),
                })

        return request.render("runbot.sticky-dashboard", qctx)

    def build_info(self, build):
        if build.state == 'duplicate':
            real_build = build.duplicate_id
        else:
            real_build = build

        return {
            'id': build.id,
            'name': build.name,
            'state': real_build.state,
            'result': real_build.result,
            'subject': build.subject,
            'author': build.author,
            'committer': build.committer,
            'dest': build.dest,
            'real_dest': real_build.dest,
            'job_age': s2human(real_build.job_age),
            'job_time': s2human(real_build.job_time),
            'job': real_build.job,
            'domain': real_build.domain,
            'host': real_build.host,
            'port': real_build.port,
            'subject': build.subject,
            'server_match': real_build.server_match,
        }

    @http.route([
        '/runbot/build/<build_id>'], type='http', auth="public", website=True)
    def build(self, build_id=None, search=None, **post):
        env = request.env

        Build = env['runbot.build']
        Logging = env['ir.logging']

        build = Build.browse(build_id)
        if not build.exists():
            return request.not_found()

        if build.state == 'duplicate':
            real_build = build.duplicate_id
        else:
            real_build = build

        # other builds
        other_builds = Build.search([('branch_id', '=', build.branch_id.id)])

        domain = ['|',
                  ('dbname', '=like', '%s-%%' % real_build.dest),
                  ('build_id', '=', real_build.id)]
        if search:
            domain.append(('name', 'ilike', search))
        logs = Logging.sudo().search(domain)

        context = {
            'repo': build.repo_id,
            'build': self.build_info(build),
            'br': {'branch': build.branch_id},
            'logs': logs,
            'other_builds': other_builds
        }
        return request.render("runbot.build", context)

    @http.route([
        '/runbot/build/<build_id>/force'],
        type='http', auth="public", methods=['POST'])
    def build_force(self, build_id, **post):
        env = request.env
        repo_id = env['runbot.build'].force(build_id)
        return request.redirect('/runbot/repo/%s' % repo_id)

    @http.route([
        '/runbot/badge/<int:repo_id>/<branch>.svg',
        '/runbot/badge/<any(default,flat):theme>/<int:repo_id>/<branch>.svg',
    ], type="http", auth="public", methods=['GET', 'HEAD'])
    def badge(self, repo_id, branch, theme='default'):

        domain = [('repo_id', '=', repo_id),
                  ('branch_id.branch_name', '=', branch),
                  ('branch_id.sticky', '=', True),
                  ('state', 'in', ['testing', 'running', 'done']),
                  ('result', '!=', 'skipped'),
                  ]

        last_update = '__last_update'
        builds = request.env['runbot.build'].sudo().search_read(
            domain, ['state', 'result', 'job_age', last_update],
            order='id desc', limit=1)

        if not builds:
            return request.not_found()

        build = builds[0]
        etag = request.httprequest.headers.get('If-None-Match')
        retag = hashlib.md5(build[last_update]).hexdigest()

        if etag == retag:
            return werkzeug.wrappers.Response(status=304)

        if build['state'] == 'testing':
            state = 'testing'
            cache_factor = 1
        else:
            cache_factor = 2
            if build['result'] == 'ok':
                state = 'success'
            elif build['result'] == 'warn':
                state = 'warning'
            else:
                state = 'failed'

        # from https://github.com/badges/shields/blob/master/colorscheme.json
        color = {
            'testing': "#dfb317",
            'success': "#4c1",
            'failed': "#e05d44",
            'warning': "#fe7d37",
        }[state]

        def text_width(s):
            fp = FontProperties(family='DejaVu Sans', size=11)
            w, h, d = TextToPath().get_text_width_height_descent(s, fp, False)
            return int(w + 1)

        class Text(object):
            __slot__ = ['text', 'color', 'width']

            def __init__(self, text, color):
                self.text = text
                self.color = color
                self.width = text_width(text) + 10

        data = {
            'left': Text(branch, '#555'),
            'right': Text(state, color),
        }
        five_minutes = 5 * 60
        headers = [
            ('Content-Type', 'image/svg+xml'),
            ('Cache-Control', 'max-age=%d' % (five_minutes * cache_factor,)),
            ('ETag', retag),
        ]
        return request.render("runbot.badge_" + theme, data, headers=headers)

    @http.route([
        '/runbot/b/<branch_name>',
        '/runbot/<model("runbot.repo"):repo>/<branch_name>'],
        type='http', auth="public", website=True)
    def fast_launch(self, branch_name=False, repo=False, **post):
        env = request.env
        Build = env['runbot.build']

        domain = [('branch_id.branch_name', '=', branch_name)]

        if repo:
            domain.extend([('branch_id.repo_id', '=', repo.id)])
            order = "sequence desc"
        else:
            order = 'repo_id ASC, sequence DESC'

        # Take the 10 lasts builds to find at least 1 running... Else no luck
        builds = Build.search(domain, order=order, limit=10)

        if builds:
            last_build = False
            for build in builds:
                if build.state == 'running' or \
                        (build.state == 'duplicate' and
                         build.duplicate_id.state == 'running'):
                    last_build = build if build.state == 'running' else \
                        build.duplicate_id
                    break

            if not last_build:
                # Find the last build regardless the state to propose a rebuild
                last_build = builds[0]

            if last_build.state != 'running':
                url = "/runbot/build/%s?ask_rebuild=1" % last_build.id
            else:
                url = ("http://%s/login?db=%s-all&login=admin&key=admin%s" %
                       (last_build.domain, last_build.dest,
                        "&redirect=/web?debug=1"
                        if not build.branch_id.branch_name.startswith('7.0')
                        else ''))
        else:
            return request.not_found()
        return request.redirect(url)
