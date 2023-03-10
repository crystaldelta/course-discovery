import datetime
import urllib.parse

import ddt
import pytz
from django.urls import reverse

from course_discovery.apps.api import serializers
from course_discovery.apps.api.v1.tests.test_views import mixins
from course_discovery.apps.api.v1.views.search import TypeaheadSearchView
from course_discovery.apps.core.tests.factories import PartnerFactory
from course_discovery.apps.core.tests.mixins import ElasticsearchTestMixin
from course_discovery.apps.course_metadata.choices import CourseRunStatus, ProgramStatus
from course_discovery.apps.course_metadata.models import CourseRun
from course_discovery.apps.course_metadata.tests.factories import (
    CourseFactory, CourseRunFactory, OrganizationFactory, ProgramFactory
)


@ddt.ddt
class CourseRunSearchViewSetTests(mixins.SerializationMixin, mixins.LoginMixin, ElasticsearchTestMixin,
                                  mixins.APITestCase):
    """ Tests for CourseRunSearchViewSet. """
    detailed_path = reverse('api:v1:search-course_runs-details')
    faceted_path = reverse('api:v1:search-course_runs-facets')
    list_path = reverse('api:v1:search-course_runs-list')

    def get_response(self, query=None, path=None):
        qs = urllib.parse.urlencode({'q': query}) if query else ''
        path = path or self.list_path
        url = '{path}?{qs}'.format(path=path, qs=qs)
        return self.client.get(url)

    def build_facet_url(self, params):
        return 'http://testserver.fake{path}?{query}'.format(
            path=self.faceted_path, query=urllib.parse.urlencode(params)
        )

    def assert_successful_search(self, path=None, serializer=None):
        """ Asserts the search functionality returns results for a generated query. """
        # Generate data that should be indexed and returned by the query
        course_run = CourseRunFactory(course__partner=self.partner, course__title='Software Testing',
                                      status=CourseRunStatus.Published)
        response = self.get_response('software', path=path)

        assert response.status_code == 200
        response_data = response.json()

        # Validate the search results
        expected = {
            'count': 1,
            'results': [
                self.serialize_course_run_search(course_run, serializer=serializer)
            ]
        }
        actual = response_data['objects'] if path == self.faceted_path else response_data
        self.assertDictContainsSubset(expected, actual)

        return course_run, response_data

    def assert_response_includes_availability_facets(self, response_data):
        """ Verifies the query facet counts/URLs are properly rendered. """
        expected = {
            'availability_archived': {
                'count': 1,
                'narrow_url': self.build_facet_url({'selected_query_facets': 'availability_archived'})
            },
            'availability_current': {
                'count': 1,
                'narrow_url': self.build_facet_url({'selected_query_facets': 'availability_current'})
            },
            'availability_starting_soon': {
                'count': 1,
                'narrow_url': self.build_facet_url({'selected_query_facets': 'availability_starting_soon'})
            },
            'availability_upcoming': {
                'count': 1,
                'narrow_url': self.build_facet_url({'selected_query_facets': 'availability_upcoming'})
            },
        }
        self.assertDictContainsSubset(expected, response_data['queries'])

    @ddt.data(faceted_path, list_path, detailed_path)
    def test_authentication(self, path):
        """ Verify the endpoint requires authentication. """
        self.client.logout()
        response = self.get_response(path=path)
        assert response.status_code == 403

    @ddt.data(
        (list_path, serializers.CourseRunSearchSerializer,),
        (detailed_path, serializers.CourseRunSearchModelSerializer,),
    )
    @ddt.unpack
    def test_search(self, path, serializer):
        """ Verify the view returns search results. """
        self.assert_successful_search(path=path, serializer=serializer)

    def test_faceted_search(self):
        """ Verify the view returns results and facets. """
        course_run, response_data = self.assert_successful_search(path=self.faceted_path)

        # Validate the pacing facet
        expected = {
            'text': course_run.pacing_type,
            'count': 1,
        }
        self.assertDictContainsSubset(expected, response_data['fields']['pacing_type'][0])

    def test_invalid_query_facet(self):
        """ Verify the endpoint returns HTTP 400 if an invalid facet is requested. """
        facet = 'not-a-facet'
        url = '{path}?selected_query_facets={facet}'.format(path=self.faceted_path, facet=facet)

        response = self.client.get(url)
        assert response.status_code == 400

        response_data = response.json()
        expected = {'detail': 'The selected query facet [{facet}] is not valid.'.format(facet=facet)}
        assert response_data == expected

    def test_availability_faceting(self):
        """ Verify the endpoint returns availability facets with the results. """
        now = datetime.datetime.now(pytz.UTC)
        archived = CourseRunFactory(course__partner=self.partner, start=now - datetime.timedelta(weeks=2),
                                    end=now - datetime.timedelta(weeks=1), status=CourseRunStatus.Published)
        current = CourseRunFactory(course__partner=self.partner, start=now - datetime.timedelta(weeks=2),
                                   end=now + datetime.timedelta(weeks=1), status=CourseRunStatus.Published)
        starting_soon = CourseRunFactory(course__partner=self.partner, start=now + datetime.timedelta(days=10),
                                         end=now + datetime.timedelta(days=90), status=CourseRunStatus.Published)
        upcoming = CourseRunFactory(course__partner=self.partner, start=now + datetime.timedelta(days=61),
                                    end=now + datetime.timedelta(days=90), status=CourseRunStatus.Published)

        response = self.get_response(path=self.faceted_path)
        assert response.status_code == 200
        response_data = response.json()

        # Verify all course runs are returned
        assert response_data['objects']['count'] == 4

        for run in [archived, current, starting_soon, upcoming]:
            serialized = self.serialize_course_run_search(run)
            # Force execution of lazy function.
            serialized['availability'] = serialized['availability'].strip()
            assert serialized in response_data['objects']['results']

        self.assert_response_includes_availability_facets(response_data)

        # Verify the results can be filtered based on availability
        url = '{path}?page=1&selected_query_facets={facet}'.format(
            path=self.faceted_path, facet='availability_archived'
        )
        response = self.client.get(url)
        assert response.status_code == 200
        response_data = response.json()
        assert response_data['objects']['results'] == [self.serialize_course_run_search(archived)]

    @ddt.data(
        (list_path, serializers.CourseRunSearchSerializer,
         ['results', 0, 'program_types', 0], ProgramStatus.Deleted, 8),
        (list_path, serializers.CourseRunSearchSerializer,
         ['results', 0, 'program_types', 0], ProgramStatus.Unpublished, 8),
        (detailed_path, serializers.CourseRunSearchModelSerializer,
         ['results', 0, 'programs', 0, 'type'], ProgramStatus.Deleted, 40),
        (detailed_path, serializers.CourseRunSearchModelSerializer,
         ['results', 0, 'programs', 0, 'type'], ProgramStatus.Unpublished, 42),
    )
    @ddt.unpack
    def test_exclude_unavailable_program_types(self, path, serializer, result_location_keys, program_status,
                                               expected_queries):
        """ Verify that unavailable programs do not show in the program_types representation. """
        course_run = CourseRunFactory(course__partner=self.partner, course__title='Software Testing',
                                      status=CourseRunStatus.Published)
        active_program = ProgramFactory(courses=[course_run.course], status=ProgramStatus.Active)
        ProgramFactory(courses=[course_run.course], status=program_status)
        self.reindex_courses(active_program)

        with self.assertNumQueries(expected_queries):
            response = self.get_response('software', path=path)
            assert response.status_code == 200
            response_data = response.json()

            # Validate the search results
            expected = {
                'count': 1,
                'results': [
                    self.serialize_course_run_search(course_run, serializer=serializer)
                ]
            }
            self.assertDictContainsSubset(expected, response_data)

            # Check that the program is indeed the active one.
            for key in result_location_keys:
                response_data = response_data[key]
            assert response_data == active_program.type.name

    @ddt.data(
        ([{'title': 'Software Testing', 'excluded': True}], 6),
        ([{'title': 'Software Testing', 'excluded': True}, {'title': 'Software Testing 2', 'excluded': True}], 7),
        ([{'title': 'Software Testing', 'excluded': False}, {'title': 'Software Testing 2', 'excluded': False}], 7),
        ([{'title': 'Software Testing', 'excluded': True}, {'title': 'Software Testing 2', 'excluded': True},
         {'title': 'Software Testing 3', 'excluded': False}], 8),
    )
    @ddt.unpack
    def test_excluded_course_run(self, course_runs, expected_queries):
        course_list = []
        course_run_list = []
        excluded_course_run_list = []
        non_excluded_course_run_list = []
        for run in course_runs:
            course_run = CourseRunFactory(course__partner=self.partner, course__title=run['title'],
                                          status=CourseRunStatus.Published)
            course_list.append(course_run.course)
            course_run_list.append(course_run)
            if run['excluded']:
                excluded_course_run_list.append(course_run)
            else:
                non_excluded_course_run_list.append(course_run)

        program = ProgramFactory(
            courses=course_list,
            status=ProgramStatus.Active,
            excluded_course_runs=excluded_course_run_list
        )
        self.reindex_courses(program)

        with self.assertNumQueries(expected_queries):
            response = self.get_response('software', path=self.list_path)

        assert response.status_code == 200
        response_data = response.json()

        assert response_data['count'] == len(course_run_list)
        for result in response_data['results']:
            for course_run in excluded_course_run_list:
                if result.get('title') == course_run.title:
                    assert result.get('program_types') == []

            for course_run in non_excluded_course_run_list:
                if result.get('title') == course_run.title:
                    assert result.get('program_types') == course_run.program_types


@ddt.ddt
class AggregateSearchViewSetTests(mixins.SerializationMixin, mixins.LoginMixin, ElasticsearchTestMixin,
                                  mixins.SynonymTestMixin, mixins.APITestCase):
    path = reverse('api:v1:search-all-facets')

    def get_response(self, query=None):
        qs = ''

        if query:
            qs = urllib.parse.urlencode(query)

        url = '{path}?{qs}'.format(path=self.path, qs=qs)
        return self.client.get(url)

    def process_response(self, response):
        response = self.get_response(response).json()
        objects = response['objects']
        assert objects['count'] > 0
        return objects

    def test_results_only_include_published_objects(self):
        """ Verify the search results only include items with status set to 'Published'. """
        # These items should NOT be in the results
        CourseRunFactory(course__partner=self.partner, status=CourseRunStatus.Unpublished)
        ProgramFactory(partner=self.partner, status=ProgramStatus.Unpublished)

        course_run = CourseRunFactory(course__partner=self.partner, status=CourseRunStatus.Published)
        program = ProgramFactory(partner=self.partner, status=ProgramStatus.Active)

        response = self.get_response()
        assert response.status_code == 200
        response_data = response.json()
        assert response_data['objects']['results'] == \
            [self.serialize_program_search(program), self.serialize_course_run_search(course_run)]

    def test_hidden_runs_excluded(self):
        """Search results should not include hidden runs."""
        visible_run = CourseRunFactory(course__partner=self.partner)
        hidden_run = CourseRunFactory(course__partner=self.partner, hidden=True)

        assert CourseRun.objects.get(hidden=True) == hidden_run

        response = self.get_response()
        data = response.json()
        assert data['objects']['results'] == [self.serialize_course_run_search(visible_run)]

    def test_results_filtered_by_default_partner(self):
        """ Verify the search results only include items related to the default partner if no partner is
        specified on the request. If a partner is included, the data should be filtered to the requested partner. """
        course_run = CourseRunFactory(course__partner=self.partner, status=CourseRunStatus.Published)
        program = ProgramFactory(partner=self.partner, status=ProgramStatus.Active)

        # This data should NOT be in the results
        other_partner = PartnerFactory()
        other_course_run = CourseRunFactory(course__partner=other_partner, status=CourseRunStatus.Published)
        other_program = ProgramFactory(partner=other_partner, status=ProgramStatus.Active)
        assert other_program.partner.short_code != self.partner.short_code
        assert other_course_run.course.partner.short_code != self.partner.short_code

        response = self.get_response()
        assert response.status_code == 200
        response_data = response.json()
        assert response_data['objects']['results'] == \
            [self.serialize_program_search(program), self.serialize_course_run_search(course_run)]

        # Filter results by partner
        response = self.get_response({'partner': other_partner.short_code})
        assert response.status_code == 200
        response_data = response.json()
        assert response_data['objects']['results'] == \
            [self.serialize_program_search(other_program), self.serialize_course_run_search(other_course_run)]

    def test_empty_query(self):
        """ Verify, when the query (q) parameter is empty, the endpoint behaves as if the parameter
        was not provided. """
        course_run = CourseRunFactory(course__partner=self.partner, status=CourseRunStatus.Published)
        program = ProgramFactory(partner=self.partner, status=ProgramStatus.Active)

        response = self.get_response({'q': '', 'content_type': ['courserun', 'program']})
        assert response.status_code == 200
        response_data = response.json()
        assert response_data['objects']['results'] == \
            [self.serialize_program_search(program), self.serialize_course_run_search(course_run)]

    @ddt.data('start', '-start')
    def test_results_ordered_by_start_date(self, ordering):
        """ Verify the search results can be ordered by start date """
        now = datetime.datetime.now(pytz.UTC)
        archived = CourseRunFactory(course__partner=self.partner, start=now - datetime.timedelta(weeks=2))
        current = CourseRunFactory(course__partner=self.partner, start=now - datetime.timedelta(weeks=1))
        starting_soon = CourseRunFactory(course__partner=self.partner, start=now + datetime.timedelta(weeks=3))
        upcoming = CourseRunFactory(course__partner=self.partner, start=now + datetime.timedelta(weeks=4))
        course_run_keys = [course_run.key for course_run in [archived, current, starting_soon, upcoming]]

        response = self.get_response({"ordering": ordering})
        assert response.status_code == 200
        assert response.data['objects']['count'] == 4

        course_runs = CourseRun.objects.filter(key__in=course_run_keys).order_by(ordering)
        expected = [self.serialize_course_run_search(course_run) for course_run in course_runs]
        assert response.data['objects']['results'] == expected

    def test_results_include_aggregation_key(self):
        """ Verify the search results only include the aggregation_key for each document. """
        course_run = CourseRunFactory(course__partner=self.partner, status=CourseRunStatus.Published)
        program = ProgramFactory(partner=self.partner, status=ProgramStatus.Active)

        response = self.get_response()
        assert response.status_code == 200
        response_data = response.json()

        expected = sorted(
            ['courserun:{}'.format(course_run.course.key), 'program:{}'.format(program.uuid)]
        )
        actual = sorted(
            [obj.get('aggregation_key') for obj in response_data['objects']['results']]
        )
        assert expected == actual


class AggregateCatalogSearchViewSetTests(mixins.SerializationMixin, mixins.LoginMixin, ElasticsearchTestMixin,
                                         mixins.APITestCase):
    path = reverse('api:v1:search-all-list')

    def test_post(self):
        """
        Verify that POST request works as expected for `AggregateSearchViewSet`
        """
        CourseFactory(key='course:edX+DemoX', title='ABCs of ??????????????')
        data = {'content_type': 'course', 'aggregation_key': ['course:edX+DemoX']}
        expected = {'previous': None, 'results': [], 'next': None, 'count': 0}
        response = self.client.post(self.path, data=data, format='json')
        assert response.json() == expected

    def test_get(self):
        """
        Verify that GET request works as expected for `AggregateSearchViewSet`
        """
        CourseFactory(key='course:edX+DemoX', title='ABCs of ??????????????')
        expected = {'previous': None, 'results': [], 'next': None, 'count': 0}
        query = {'content_type': 'course', 'aggregation_key': ['course:edX+DemoX']}
        qs = urllib.parse.urlencode(query)
        url = '{path}?{qs}'.format(path=self.path, qs=qs)
        response = self.client.get(url)
        assert response.json() == expected


class TypeaheadSearchViewTests(mixins.TypeaheadSerializationMixin, mixins.LoginMixin, ElasticsearchTestMixin,
                               mixins.SynonymTestMixin, mixins.APITestCase):
    path = reverse('api:v1:search-typeahead')

    def get_response(self, query=None, partner=None):
        query_dict = query or {}
        query_dict.update({'partner': partner or self.partner.short_code})
        qs = urllib.parse.urlencode(query_dict)

        url = '{path}?{qs}'.format(path=self.path, qs=qs)
        return self.client.get(url)

    def process_response(self, response):
        response = self.get_response(response).json()
        self.assertTrue(response['course_runs'] or response['programs'])
        return response

    def test_typeahead(self):
        """ Test typeahead response. """
        title = "Python"
        course_run = CourseRunFactory(title=title, course__partner=self.partner)
        program = ProgramFactory(title=title, status=ProgramStatus.Active, partner=self.partner)
        response = self.get_response({'q': title})
        self.assertEqual(response.status_code, 200)
        response_data = response.json()
        self.assertDictEqual(response_data, {'course_runs': [self.serialize_course_run_search(course_run)],
                                             'programs': [self.serialize_program_search(program)]})

    def test_typeahead_multiple_results(self):
        """ Verify the typeahead responses always returns a limited number of results, even if there are more hits. """
        RESULT_COUNT = TypeaheadSearchView.RESULT_COUNT
        title = "Test"
        for i in range(RESULT_COUNT + 1):
            CourseRunFactory(title="{}{}".format(title, i), course__partner=self.partner)
            ProgramFactory(title="{}{}".format(title, i), status=ProgramStatus.Active, partner=self.partner)
        response = self.get_response({'q': title})
        self.assertEqual(response.status_code, 200)
        response_data = response.json()
        self.assertEqual(len(response_data['course_runs']), RESULT_COUNT)
        self.assertEqual(len(response_data['programs']), RESULT_COUNT)

    def test_typeahead_deduplicate_course_runs(self):
        """ Verify the typeahead response will only include the first course run per course. """
        RESULT_COUNT = TypeaheadSearchView.RESULT_COUNT
        title = "Test"
        course1 = CourseFactory(partner=self.partner)
        course2 = CourseFactory(partner=self.partner)
        for i in range(RESULT_COUNT):
            CourseRunFactory(title="{}{}{}".format(title, course1.title, i), course=course1)
        for i in range(RESULT_COUNT):
            CourseRunFactory(title="{}{}{}".format(title, course2.title, i), course=course2)
        response = self.get_response({'q': title})
        assert response.status_code == 200
        response_data = response.json()

        # There are many runs for both courses, but only one from each will be included
        course_runs = response_data['course_runs']
        assert len(course_runs) == 2
        # compare course titles embedded in course run title to ensure that course runs belong to different courses
        assert course_runs[0]['title'][4:-1] != course_runs[1]['title'][4:-1]

    def test_typeahead_multiple_authoring_organizations(self):
        """ Test typeahead response with multiple authoring organizations. """
        title = "Design"
        authoring_organizations = OrganizationFactory.create_batch(3)
        course_run = CourseRunFactory(
            title=title,
            authoring_organizations=authoring_organizations,
            course__partner=self.partner
        )
        program = ProgramFactory(
            title=title, authoring_organizations=authoring_organizations,
            status=ProgramStatus.Active, partner=self.partner
        )
        response = self.get_response({'q': title})
        self.assertEqual(response.status_code, 200)
        response_data = response.json()
        self.assertDictEqual(response_data, {'course_runs': [self.serialize_course_run_search(course_run)],
                                             'programs': [self.serialize_program_search(program)]})

    def test_partial_term_search(self):
        """ Test typeahead response with partial term search. """
        title = "Learn Data Science"
        course_run = CourseRunFactory(title=title, course__partner=self.partner)
        program = ProgramFactory(title=title, status=ProgramStatus.Active, partner=self.partner)
        query = "Data Sci"
        response = self.get_response({'q': query})
        self.assertEqual(response.status_code, 200)
        response_data = response.json()
        expected_response_data = {
            'course_runs': [self.serialize_course_run_search(course_run)],
            'programs': [self.serialize_program_search(program)]
        }
        self.assertDictEqual(response_data, expected_response_data)

    def test_unpublished_and_hidden_courses(self):
        """ Verify that typeahead does not return unpublished or hidden courses
        or programs that are not active. """
        title = "supply"
        course_run = CourseRunFactory(title=title, course__partner=self.partner)
        CourseRunFactory(title=title + "unpublished", status=CourseRunStatus.Unpublished, course__partner=self.partner)
        CourseRunFactory(title=title + "hidden", hidden=True, course__partner=self.partner)
        program = ProgramFactory(title=title, status=ProgramStatus.Active, partner=self.partner)
        ProgramFactory(title=title + "unpublished", status=ProgramStatus.Unpublished, partner=self.partner)
        query = "suppl"
        response = self.get_response({'q': query})
        self.assertEqual(response.status_code, 200)
        response_data = response.json()
        expected_response_data = {
            'course_runs': [self.serialize_course_run_search(course_run)],
            'programs': [self.serialize_program_search(program)]
        }
        self.assertDictEqual(response_data, expected_response_data)

    def test_typeahead_hidden_programs(self):
        """ Verify that typeahead does not return hidden programs. """
        title = "hiddenprogram"
        program = ProgramFactory(title=title, hidden=False, status=ProgramStatus.Active, partner=self.partner)
        ProgramFactory(title=program.title + 'hidden', hidden=True, status=ProgramStatus.Active, partner=self.partner)
        response = self.get_response({'q': program.title})
        self.assertEqual(response.status_code, 200)
        response_data = response.json()
        expected_response_data = {
            'course_runs': [],
            'programs': [self.serialize_program_search(program)]
        }
        self.assertDictEqual(response_data, expected_response_data)

    def test_exception(self):
        """ Verify the view raises an error if the 'q' query string parameter is not provided. """
        response = self.get_response()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, ["The 'q' querystring parameter is required for searching."])

    def test_typeahead_authoring_organizations_partial_search(self):
        """ Test typeahead response with partial organization matching. """
        authoring_organizations = OrganizationFactory.create_batch(3)
        course_run = CourseRunFactory(authoring_organizations=authoring_organizations, course__partner=self.partner)
        program = ProgramFactory(authoring_organizations=authoring_organizations, partner=self.partner)
        partial_key = authoring_organizations[0].key[0:5]

        response = self.get_response({'q': partial_key})
        self.assertEqual(response.status_code, 200)
        expected = {
            'course_runs': [self.serialize_course_run_search(course_run)],
            'programs': [self.serialize_program_search(program)]
        }
        self.assertDictEqual(response.data, expected)

    def test_typeahead_org_course_runs_come_up_first(self):
        """ Test typeahead response to ensure org is taken into account. """
        MITx = OrganizationFactory(key='MITx')
        HarvardX = OrganizationFactory(key='HarvardX')
        mit_run = CourseRunFactory(
            authoring_organizations=[MITx, HarvardX],
            title='MIT Testing1',
            course__partner=self.partner
        )
        harvard_run = CourseRunFactory(
            authoring_organizations=[HarvardX],
            title='MIT Testing2',
            course__partner=self.partner
        )
        mit_program = ProgramFactory(
            authoring_organizations=[MITx, HarvardX],
            title='MIT Testing1',
            partner=self.partner
        )
        harvard_program = ProgramFactory(
            authoring_organizations=[HarvardX],
            title='MIT Testing2',
            partner=self.partner
        )
        response = self.get_response({'q': 'mit'})
        self.assertEqual(response.status_code, 200)
        expected = {
            'course_runs': [self.serialize_course_run_search(mit_run),
                            self.serialize_course_run_search(harvard_run)],
            'programs': [self.serialize_program_search(mit_program),
                         self.serialize_program_search(harvard_program)]
        }
        self.assertDictEqual(response.data, expected)
